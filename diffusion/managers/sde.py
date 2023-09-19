from torchmanager import losses, metrics
from torchmanager_core import torch
from torchmanager_core.typing import Any, Generic, Optional, Union, TypeVar

from diffusion.data import DiffusionData
from diffusion.nn import DiffusionModule
from diffusion.scheduling import BetaSpace
from diffusion.sde import SDE, SubVPSDE, VESDE, VPSDE
from .diffusion import DiffusionManager

Module = TypeVar("Module", bound=DiffusionModule)
SDEType = TypeVar("SDEType", bound=SDE)


class SDEManager(DiffusionManager[Module], Generic[Module, SDEType]):
    beta_space: Optional[BetaSpace]
    is_continous: bool
    sde: SDEType

    def __init__(self, model: Module, /, sde: SDEType, time_steps: int, beta_space: Optional[BetaSpace] = None, *, is_continous: bool = False, optimizer: Optional[torch.optim.Optimizer] = None, loss_fn: Optional[Union[losses.Loss, dict[str, losses.Loss]]] = None, metrics: dict[str, metrics.Metric] = ...) -> None:
        super().__init__(model, time_steps, optimizer, loss_fn, metrics)
        self.beta_space = beta_space
        self.is_continous = is_continous
        self.sde = sde

        # check parameters
        if isinstance(self.sde, VPSDE) and self.beta_space is None:
            raise ValueError("Beta space is required for VPSDE.")

    def forward(self, x_train: DiffusionData, y_train: Optional[torch.Tensor] = None) -> tuple[torch.Tensor, Optional[torch.Tensor]]:
        # Scale neural network output by standard deviation and flip sign
        # For VE-trained models, t=0 corresponds to the highest noise level
        if isinstance(self.sde, SubVPSDE) or (self.is_continous and isinstance(self.sde, VPSDE)):
            t = x_train.t * 999
            _, std = self.sde.marginal_prob(torch.zeros_like(x_train.x), x_train.t)
        elif isinstance(self.sde, VPSDE):
            assert self.beta_space is not None, "Beta space is required for VPSDE."
            t = x_train.t * (self.sde.N - 1)
            std = self.beta_space.sqrt_one_minus_alphas_cumprod
        elif self.is_continous and isinstance(self.sde, VESDE):
            _, t = self.sde.marginal_prob(torch.zeros_like(x_train.x), x_train.t)
            std = 1
        elif isinstance(self.sde, VESDE):
            t = self.sde.T - x_train.t
            t *= self.sde.N - 1
            t = t.round().long()
            std = 1
        else:
            raise NotImplementedError(f"SDE class {type(self.sde)} not yet supported.")

        # calculate using score function
        x = DiffusionData(x_train.x, t, condition=x_train.condition)
        score = self.model(x)
        y = score / std

        # calculate loss
        loss = self.compiled_losses(y, y_train) if self.loss_fn is not None and y_train is not None else None
        return y, loss

    def forward_diffusion(self, data: Any, condition: Optional[torch.Tensor] = None, t: Optional[torch.Tensor] = None) -> tuple[Any, torch.Tensor]:
        t = torch.randint(0, self.time_steps, (data.shape[0],), device=data.device).long() if t is None else t.to(data.device)
        z = torch.randn_like(data, device=t.device)
        mean, std = self.sde.marginal_prob(data, t)
        x = mean + std[:, None, None, None] * z
        noise = z / std[:, None, None, None]
        return DiffusionData(x, t), noise

    def to(self, device: torch.device) -> None:
        if self.beta_space is not None:
            self.beta_space = self.beta_space.to(device)
        return super().to(device)

    def sampling_step(self, data: DiffusionData, i, /, *, return_noise: bool = False) -> Union[torch.Tensor, tuple[torch.Tensor, torch.Tensor]]:
        # predict
        if isinstance(self.sde, VESDE):
            # The ancestral sampling predictor for VESDE
            timestep = (data.t * (self.sde.N - 1) / self.sde.T).long()
            sigma = self.sde.discrete_sigmas[timestep]
            adjacent_sigma = torch.where(timestep == 0, torch.zeros_like(data.t), self.sde.discrete_sigmas.to(data.t.device)[timestep - 1])
            predicted_noise, _ = score, _ = self.forward(data)
            x_mean = data.x + score * (sigma ** 2 - adjacent_sigma ** 2)[:, None, None, None]
            std = torch.sqrt((adjacent_sigma ** 2 * (sigma ** 2 - adjacent_sigma ** 2)) / (sigma ** 2))
            noise = torch.randn_like(data.x)
            y = x_mean + std[:, None, None, None] * noise
        elif isinstance(self.sde, VPSDE):
            # The ancestral sampling predictor for VESDE
            assert self.beta_space is not None, "Beta space is required for VPSDE."
            timestep = (data.t * (self.sde.N - 1) / self.sde.T).long()
            beta = self.beta_space.sample_betas(timestep, data.x.shape)
            predicted_noise, _ = score, _ = self.forward(data)
            x_mean = (data.x + beta[:, None, None, None] * score) / torch.sqrt(1. - beta)[:, None, None, None]
            noise = torch.randn_like(data.x)
            y = x_mean + torch.sqrt(beta)[:, None, None, None] * noise
        else:
            # The traditional reverse diffusion predictor
            f, G = self.sde.discretize(data.x, data.t)
            f = f - G[:, None, None, None] ** 2 * self.model(data) * 0.5
            G = torch.zeros_like(G)
            z = torch.randn_like(data.x)
            predicted_noise = x_mean = data.x - f
            y = x_mean + G[:, None, None, None] * z
        return (y, predicted_noise) if return_noise else y
