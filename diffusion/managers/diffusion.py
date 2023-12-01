from torch.nn.utils import clip_grad
from torch.utils.data import DataLoader
from torchmanager import losses, metrics, Manager as _Manager
from torchmanager.data import Dataset
from torchmanager_core import abc, devices, errors, torch, view, _raise
from torchmanager_core.typing import Any, Iterable, Module, Optional, Union

from diffusion.data import DiffusionData


class DiffusionManager(_Manager[Module], abc.ABC):
    """
    A torchmanager `Manager` for diffusion model

    * Abstract class
    * Extends: `torchmanager.Manager`

    - Properties:
        - time_steps: An `int` of total time steps
    - Methods to implement:
        - forward_diffusion: Forward pass of diffusion model, sample noises
        - sampling: Samples a given number of images
    """
    time_steps: int

    def __init__(self, model: Module, time_steps: int, optimizer: Optional[torch.optim.Optimizer] = None, loss_fn: Optional[Union[losses.Loss, dict[str, losses.Loss]]] = None, metrics: dict[str, metrics.Metric] = {}) -> None:
        """
        Constructor

        - Prarameters:
            - model: An optional target `torch.nn.Module` to be trained
            - time_steps: An `int` of total number of steps
            - optimizer: An optional `torch.optim.Optimizer` to train the model
            - loss_fn: An optional `Loss` object to calculate the loss for single loss or a `dict` of losses in `Loss` with their names in `str` to calculate multiple losses
            - metrics: An optional `dict` of metrics with a name in `str` and a `Metric` object to calculate the metric
        """
        # initialize
        super().__init__(model, optimizer, loss_fn, metrics)
        self.time_steps = time_steps

    def backward(self, loss: torch.Tensor) -> None:
        super().backward(loss)
        clip_grad.clip_grad_norm_(self.model.parameters(), max_norm=1)
        self.compiled_optimizer.step()

    @abc.abstractmethod
    def forward_diffusion(self, data: Any, condition: Optional[torch.Tensor] = None, t: Optional[torch.Tensor] = None) -> tuple[Any, torch.Tensor]:
        """
        Forward pass of diffusion model, sample noises

        - Parameters:
            - data: Any kind of clear data
            - condition: An optional `torch.Tensor` of the condition to generate images
            - t: An optional `torch.Tensor` of the time step, sampling uniformly if not given
        - Returns: A `tuple` of noisy images and sampled time step in `DiffusionData` and noises in `torch.Tensor`
        """
        return NotImplemented

    @torch.no_grad()
    def predict(self, num_images: int, image_size: Union[int, tuple[int, ...]], condition: Optional[torch.Tensor] = None, noises: Optional[torch.Tensor] = None, device: Optional[Union[torch.device, list[torch.device]]] = None, use_multi_gpus: bool = False, show_verbose: bool = False) -> list[torch.Tensor]:
        # find available device
        cpu, device, target_devices = devices.search(device)
        if device == cpu and len(target_devices) < 2:
            use_multi_gpus = False
        devices.set_default(target_devices[0])

        # initialize and format parameters
        image_size = tuple(image_size) if isinstance(image_size, Iterable) else (3, image_size, image_size)
        assert image_size[0] > 0 and image_size[1] > 0, _raise(ValueError(f"Image size must be positive numbers, got {image_size}."))
        assert num_images > 0, _raise(ValueError(f"Number of images must be a positive number, got {num_images}."))
        imgs = torch.randn([num_images] + list(image_size)) if noises is None else noises
        assert imgs.shape[0] >= num_images, _raise(ValueError(f"Number of noises ({imgs.shape[0]}) must be equal or greater than number of images to generate ({num_images})"))

        try:
            # move model to device
            if use_multi_gpus:
                self.data_parallel(target_devices)
            else:
                imgs = imgs.to(device)
            self.to(device)
            self.model.eval()

            # move condition to device
            c = devices.move_to_device(condition, device) if condition is not None else None
            if c is not None:
                assert isinstance(c, torch.Tensor), "Condition must be a valid `torch.Tensor` when given."
            return self.sampling(num_images, x_t=imgs, condition=condition, show_verbose=show_verbose)
        except Exception as error:
            view.logger.error(error)
            runtime_error = errors.PredictionError()
            raise runtime_error from error
        finally:
            # empty cache
            self.model = self.raw_model.to(cpu)
            devices.empty_cache()

    @abc.abstractmethod
    def sampling_step(self, data: DiffusionData, i, /, *, return_noise: bool = False) -> Union[torch.Tensor, tuple[torch.Tensor, torch.Tensor]]:
        """
        Sampling step of diffusion model

        - Parameters:
            - data: A `DiffusionData` object
            - i: An `int` of current time step
            - return_noise: A `bool` flag to return predicted noise
        - Returns: A `torch.Tensor` of noised image
        """
        return NotImplemented

    @torch.no_grad()
    def sampling(self, num_images: int, x_t: torch.Tensor, condition: Optional[torch.Tensor] = None, *, sampling_range: Optional[range] = None, show_verbose: bool = False) -> list[torch.Tensor]:
        '''
        Samples a given number of images

        - Parameters:
            - num_images: An `int` of number of images to generate
            - x_t: A `torch.Tensor` of the image at T step
            - condition: An optional `torch.Tensor` of the condition to generate images
            - start_index: An optional `int` of the start index of reversed time step
            - end_index: An `int` of the end index of reversed time step
            - show_verbose: A `bool` flag to show the progress bar during testing
        - Retruns: A `list` of `torch.Tensor` generated results
        '''
        # initialize
        imgs = x_t
        progress_bar = view.tqdm(desc='Sampling loop time step', total=self.time_steps) if show_verbose else None
        sampling_range = range(self.time_steps, 1) if sampling_range is None else sampling_range

        # sampling loop time step
        for i in sampling_range:
            # fetch data
            t = torch.full((num_images,), i, dtype=torch.long, device=imgs.device)

            # append to predicitions
            x = DiffusionData(imgs, t, condition=condition)
            y = self.sampling_step(x, i)
            assert isinstance(y, torch.Tensor), "The output must be a valid `torch.Tensor`."
            imgs = y.to(imgs.device)

            # update progress bar
            if progress_bar is not None:
                progress_bar.update()

        # reset model and loss
        return [img for img in imgs]

    @torch.no_grad()
    def test(self, dataset: Union[DataLoader[torch.Tensor], Dataset[torch.Tensor]], sampling_images: bool = False, sampling_shape: Optional[Union[int, tuple[int, ...]]] = None, *, sampling_range: Optional[range] = None, device: Optional[Union[torch.device, list[torch.device]]] = None, empty_cache: bool = True, use_multi_gpus: bool = False, show_verbose: bool = False) -> dict[str, float]:
        # normali testing if not sampling images
        if not sampling_images:
            return super().test(dataset, device=device, empty_cache=empty_cache, use_multi_gpus=use_multi_gpus, show_verbose=show_verbose)

        # initialize device
        cpu, device, target_devices = devices.search(device)
        if device == cpu and len(target_devices) < 2:
            use_multi_gpus = False
        devices.set_default(target_devices[0])

        # initialize
        summary: dict[str, float] = {}
        unbatched_len = dataset.batched_len if isinstance(dataset, Dataset) else len(dataset)
        progress_bar = view.tqdm(total=unbatched_len) if show_verbose else None
        sampling_range = range(self.time_steps, 1) if sampling_range is None else sampling_range

        # reset loss function and metrics
        for _, m in self.metric_fns.items():
            m.eval().reset()

        try:
            # set module status and move to device
            if use_multi_gpus:
                self.data_parallel(target_devices)
            self.to(device)
            self.model.eval()

            # batch loop
            for x_test, y_test in dataset:
                # move x_test, y_test to device
                if not use_multi_gpus:
                    x_test = devices.move_to_device(x_test, device)
                y_test = devices.move_to_device(y_test, device)
                assert isinstance(x_test, torch.Tensor), "The input must be a valid `torch.Tensor`."
                assert isinstance(y_test, torch.Tensor), "The target must be a valid `torch.Tensor`."

                # sampling
                sampling_shape = y_test.shape[-3:] if sampling_shape is None else sampling_shape
                noises = torch.randn_like(y_test, dtype=torch.float, device=y_test.device)
                x = self.sampling(int(x_test.shape[0]), x_t=noises, condition=x_test, sampling_range=sampling_range, show_verbose=False)
                x = torch.cat([img.unsqueeze(0) for img in x])
                x = devices.move_to_device(x, device)
                y_test = devices.move_to_device(y_test, device)
                step_summary: dict[str, float] = {}

                # forward metrics
                for name, fn in self.compiled_metrics.items():
                    if name.startswith("val_"):
                        name = name.replace("val_", "")
                    elif "loss" in name:
                        continue
                    try:
                        fn(x, y_test)
                        step_summary[name] = float(fn.result.detach())
                    except Exception as metric_error:
                        runtime_error = errors.MetricError(name)
                        raise runtime_error from metric_error

                # update progress bar
                if progress_bar is not None:
                    progress_bar.set_postfix(step_summary)
                    progress_bar.update()

            # summarize
            for name, fn in self.metric_fns.items():
                if name.startswith("val_"):
                    name = name.replace("val_", "")
                try:
                    summary[name] = float(fn.result.detach())
                except Exception as metric_error:
                    runtime_error = errors.MetricError(name)
                    raise runtime_error from metric_error

            # reset model and loss
            return summary
        except KeyboardInterrupt:
            view.logger.info("Testing interrupted.")
            return {}
        except Exception as error:
            view.logger.error(error)
            runtime_error = errors.TestingError()
            raise runtime_error from error
        finally:
            # close progress bar
            if progress_bar is not None:
                progress_bar.close()

            # empty cache
            if empty_cache:
                self.to(cpu)
                self.model = self.raw_model
                self.loss_fn = self.raw_loss_fn if self.raw_loss_fn is not None else self.raw_loss_fn
                devices.empty_cache()

    def to(self, device: torch.device) -> None:
        super().to(device)

    def train_step(self, x_train: torch.Tensor, y_train: torch.Tensor) -> dict[str, float]:
        x_train, noise = self.forward_diffusion(y_train.to(x_train.device), condition=x_train)
        return super().train_step(x_train, noise.to(y_train.device))

    def test_step(self, x_test: torch.Tensor, y_test: torch.Tensor) -> dict[str, float]:
        x_test, noise = self.forward_diffusion(y_test.to(x_test.device), condition=x_test)
        return super().test_step(x_test, noise.to(y_test.device))
