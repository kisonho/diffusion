import torch, unittest


class Case0100(unittest.TestCase):
    def test_fid(self):
        from diffusion.metrics import FID
        from torchvision.models import inception_v3
        import ssl

        # get feature extractor
        ssl._create_default_https_context = ssl._create_unverified_context
        inception = inception_v3(pretrained=True)
        inception.fc = torch.nn.Identity()  # type: ignore
        inception.eval()
        fid_fn = FID(inception)

        # generate fake data
        input = torch.randn(16, 3, 256, 256)
        target = torch.randn(16, 3, 256, 256)

        # run lpips
        _ = fid_fn(input, target)

        # get result
        result = float(fid_fn.result)
        self.assertGreaterEqual(result, 0)

    def test_import(self):
        import diffusion

        try:
            from packaging.version import Version # type: ignore
            self.assertGreaterEqual(diffusion.VERSION, Version("v1.0"))
        except ImportError:
            pass

    def test_lpips(self):
        from diffusion.metrics import LPIPS
        import ssl

        ssl._create_default_https_context = ssl._create_unverified_context
        lpips_fn = LPIPS()

        # generate fake data
        input = torch.randn(16, 3, 32, 32)
        target = torch.randn(16, 3, 32, 32)

        # run lpips
        _ = lpips_fn(input, target)

        # get result
        result = float(lpips_fn.result)
        self.assertGreaterEqual(result, 0)

    def test_miou(self):
        from diffusion.metrics import MIoU
        from torchvision import models

        # load metric
        seg_model = models.segmentation.deeplabv3_resnet101()
        assert isinstance(seg_model, torch.nn.Module), "The pre-trained model is not a valid PyTorch model."
        seg_model = seg_model.eval()
        miou_fn = MIoU(seg_model)

        # generate fake data
        input = torch.randn(4, 3, 256, 256)
        target = torch.randint(0, 80, size=(16, 1, 1024, 2048))
        result = float(miou_fn(input, target))
        self.assertGreaterEqual(result, 0)

    def test_scheduler(self) -> None:
        import torch
        from diffusion.scheduling import BetaScheduler, BetaSpace

        T = 1000
        beta_scheduler = BetaScheduler("linear")
        beta_space = beta_scheduler.calculate_space(T)
        correct_beta_space = BetaSpace(torch.linspace(0.0001, 0.01, T))
        eq_betas = beta_space.betas - correct_beta_space.betas
        eq_betas = eq_betas.sum()
        self.assertEqual(eq_betas, 0)
