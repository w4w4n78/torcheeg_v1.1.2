import warnings
from typing import Any, Dict, List, Tuple

import pytorch_lightning as pl
import torch
import torch.autograd as autograd
import torch.nn as nn
import torchmetrics
from torch.utils.data import DataLoader
from torchmetrics.image.inception import InceptionScore

from .utils import FrechetInceptionDistance

_EVALUATE_OUTPUT = List[Dict[str, float]]  # 1 dict per DataLoader


def gradient_penalty(model, real, fake, *args, **kwargs):
    device = real.device
    real = real.data
    fake = fake.data
    alpha = torch.rand(real.size(0), *([1] * (len(real.shape) - 1))).to(device)
    inputs = alpha * real + ((1 - alpha) * fake)
    inputs.requires_grad_()

    outputs = model(inputs, *args, **kwargs)
    gradient = autograd.grad(outputs=outputs,
                             inputs=inputs,
                             grad_outputs=torch.ones_like(outputs).to(device),
                             create_graph=True,
                             retain_graph=True,
                             only_inputs=True)[0]
    gradient = gradient.flatten(1)
    return ((gradient.norm(2, dim=1) - 1)**2).mean()


class WGANGPTrainer(pl.LightningModule):
    r'''
    This class provide the implementation for WGAN-GP. It trains a zero-sum game between the generator and the discriminator, just like the traditional generative networks. The generator is optimized to generate simulation samples that are indistinguishable by the discriminator, and the discriminator is optimized to discriminate false samples generated by the generator. Compared with vanilla GAN, with WGAN-GP we can improve the stability of learning, get rid of problems like mode collapse, and provide meaningful learning curves useful for debugging and hyperparameter searches. Thus, existing work typically uses WGAN-GP to generate simulated EEG signals. For more details, please refer to the following information. 

    - Paper: Gulrajani I, Ahmed F, Arjovsky M, et al. Improved training of wasserstein gans[J]. Advances in neural information processing systems, 2017, 30.
    - URL: https://arxiv.org/abs/1704.00028
    - Related Project: https://github.com/eriklindernoren/PyTorch-GAN

    .. code-block:: python
        
        g_model = BGenerator(in_channels=128)
        d_model = BDiscriminator(in_channels=4)
        trainer = WGANGPTrainer(generator, discriminator)
        trainer.fit(train_loader, val_loader)
        trainer.test(test_loader)

    Args:
        generator (nn.Module): The generator model for EEG signal generation, whose inputs are Gaussian distributed random vectors, outputs are generated EEG signals. The dimensions of the input vector should be defined on the :obj:`in_channel` attribute. The output layer does not need to have a softmax activation function.
        discriminator (nn.Module): The discriminator model to determine whether the EEG signal is real or generated, and the dimension of its output should be equal to the one (i.e., the score to distinguish the real and the fake). The output layer does not need to have a sigmoid activation function.
        generator_lr (float): The learning rate of the generator. (default: :obj:`0.0001`)
        discriminator_lr (float): The learning rate of the discriminator. (default: :obj:`0.0001`)
        weight_gradient_penalty (float): The weight of gradient penalty loss to trade-off between the adversarial training loss and gradient penalty loss. (default: :obj:`1.0`)
        weight_decay: (float): The weight decay (L2 penalty). (default: :obj:`0.0`)
        latent_channels (int): The dimension of the latent vector. If not specified, it will be inferred from the :obj:`in_channels` attribute of the generator. (default: :obj:`None`)
        devices (int): The number of GPUs to use. (default: :obj:`1`)
        accelerator (str): The accelerator to use. Available options are: 'cpu', 'gpu'. (default: :obj:`"cpu"`)
        metrics (List[str]): The metrics to use. The metrics to use. Available options are: 'fid', 'is'. (default: :obj:`[]`)
    
    .. automethod:: fit
    .. automethod:: test
    .. automethod:: sample
    '''

    def __init__(self,
                 generator: nn.Module,
                 discriminator: nn.Module,
                 generator_lr: float = 1e-4,
                 discriminator_lr: float = 1e-4,
                 weight_decay: float = 0.0,
                 weight_gradient_penalty: float = 1.0,
                 latent_channels: int = None,
                 devices: int = 1,
                 accelerator: str = "cpu",
                 metrics: List[str] = [],
                 metric_extractor: nn.Module = None,
                 metric_classifier: nn.Module = None,
                 metric_num_features: int = None):
        super().__init__()
        self.automatic_optimization = False

        self.generator = generator
        self.discriminator = discriminator

        self.generator_lr = generator_lr
        self.discriminator_lr = discriminator_lr
        self.weight_decay = weight_decay
        self.weight_gradient_penalty = weight_gradient_penalty

        if hasattr(generator, 'in_channels') and latent_channels is None:
            warnings.warn(
                f'No latent_channels specified, use generator.in_channels ({generator.in_channels}) as latent_channels.'
            )
            latent_channels = generator.in_channels
        assert not latent_channels is None, 'The latent_channels should be specified.'
        self.latent_channels = latent_channels

        self.devices = devices
        self.accelerator = accelerator
        self.metrics = metrics

        self.bce_fn = nn.BCEWithLogitsLoss()

        self.metric_extractor = metric_extractor
        self.metric_classifier = metric_classifier
        self.metric_num_features = metric_num_features
        self.init_metrics(metrics)

    def init_metrics(self, metrics) -> None:
        self.train_g_loss = torchmetrics.MeanMetric()
        self.train_d_loss = torchmetrics.MeanMetric()

        self.val_g_loss = torchmetrics.MeanMetric()
        self.val_d_loss = torchmetrics.MeanMetric()

        self.test_g_loss = torchmetrics.MeanMetric()
        self.test_d_loss = torchmetrics.MeanMetric()

        if 'fid' in metrics:
            assert not self.metric_extractor is None, 'The metric_extractor should be specified.'
            if hasattr(self.metric_extractor,
                       'in_channels') and self.metric_num_features is None:
                warnings.warn(
                    f'No metric_num_features specified, use metric_extractor.in_channels ({self.metric_extractor.in_channels}) as metric_num_features.'
                )
                self.metric_num_features = self.metric_extractor.in_channels
            assert not self.metric_num_features is None, 'The metric_num_features should be specified.'
            self.train_fid = FrechetInceptionDistance(self.metric_extractor,
                                                      self.metric_num_features)
            self.val_fid = FrechetInceptionDistance(self.metric_extractor,
                                                    self.metric_num_features)
            self.test_fid = FrechetInceptionDistance(self.metric_extractor,
                                                     self.metric_num_features)

        if 'is' in metrics:
            assert not self.metric_extractor is None, 'The metric_classifier should be specified.'
            self.train_is = InceptionScore(self.metric_classifier)
            self.val_is = InceptionScore(self.metric_classifier)
            self.test_is = InceptionScore(self.metric_classifier)

    def fit(self,
            train_loader: DataLoader,
            val_loader: DataLoader,
            max_epochs: int = 300,
            *args,
            **kwargs) -> Any:
        r'''
        Args:
            train_loader (DataLoader): Iterable DataLoader for traversing the training data batch (:obj:`torch.utils.data.dataloader.DataLoader`, :obj:`torch_geometric.loader.DataLoader`, etc).
            val_loader (DataLoader): Iterable DataLoader for traversing the validation data batch (:obj:`torch.utils.data.dataloader.DataLoader`, :obj:`torch_geometric.loader.DataLoader`, etc).
            max_epochs (int): Maximum number of epochs to train the model. (default: :obj:`300`)
        '''
        trainer = pl.Trainer(devices=self.devices,
                             accelerator=self.accelerator,
                             max_epochs=max_epochs,
                             inference_mode=False,
                             *args,
                             **kwargs)
        return trainer.fit(self, train_loader, val_loader)

    def test(self, test_loader: DataLoader, *args,
             **kwargs) -> _EVALUATE_OUTPUT:
        r'''
        Args:
            test_loader (DataLoader): Iterable DataLoader for traversing the test data batch (torch.utils.data.dataloader.DataLoader, torch_geometric.loader.DataLoader, etc).
        '''
        trainer = pl.Trainer(devices=self.devices,
                             accelerator=self.accelerator,
                             inference_mode=False,
                             *args,
                             **kwargs)
        return trainer.test(self, test_loader)

    def forward(self, latent: torch.Tensor) -> torch.Tensor:
        return self.generator(latent)

    def predict_step(self,
                     batch: Tuple[torch.Tensor],
                     batch_idx: int,
                     dataloader_idx: int = 0):
        x, _ = batch
        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)
        return self(latent)

    def training_step(self, batch: Tuple[torch.Tensor],
                      batch_idx: int) -> torch.Tensor:
        x, _ = batch
        generator_optimizer, discriminator_optimizer = self.optimizers()

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        # train generator
        self.toggle_optimizer(generator_optimizer)

        gen_x = self.generator(latent)
        g_loss = -torch.mean(self.discriminator(gen_x))
        g_loss.backward()

        generator_optimizer.step()
        generator_optimizer.zero_grad()
        self.untoggle_optimizer(generator_optimizer)

        # train discriminator
        self.toggle_optimizer(discriminator_optimizer)

        real_loss = self.discriminator(x)
        fake_loss = self.discriminator(gen_x.detach())
        gp_term = gradient_penalty(self.discriminator, x, gen_x)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term
        d_loss.backward()

        discriminator_optimizer.step()
        discriminator_optimizer.zero_grad()
        self.untoggle_optimizer(discriminator_optimizer)

        self.log("train_g_loss",
                 self.train_g_loss(g_loss),
                 prog_bar=True,
                 on_epoch=False,
                 logger=False,
                 on_step=True)
        self.log("train_d_loss",
                 self.train_d_loss(d_loss),
                 prog_bar=True,
                 on_epoch=False,
                 logger=False,
                 on_step=True)

        if 'fid' in self.metrics:
            self.train_fid.update(x, real=True)
            self.train_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.train_is.update(gen_x)
        # In manual optimization, `training_step` must either return a Tensor or have no return.

    def on_train_epoch_end(self) -> None:
        self.log("train_g_loss",
                 self.train_g_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)
        self.log("train_d_loss",
                 self.train_d_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)

        if 'fid' in self.metrics:
            self.log("train_fid",
                     self.train_fid.compute(),
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)
        if 'is' in self.metrics:
            self.log("train_is",
                     self.train_is.compute()[0],
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)

        # print the metrics
        str = "\n[Train] "
        for key, value in self.trainer.logged_metrics.items():
            if key.startswith("train_"):
                str += f"{key}: {value:.3f} "
        print(str + '\n')

        # reset the metrics
        self.train_g_loss.reset()
        self.train_d_loss.reset()

        if 'fid' in self.metrics:
            self.train_fid.reset()
        if 'is' in self.metrics:
            self.train_is.reset()

    @torch.enable_grad()
    def validation_step(self, batch: Tuple[torch.Tensor],
                        batch_idx: int) -> torch.Tensor:
        x, _ = batch

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        gen_x = self.generator(latent)
        g_loss = -torch.mean(self.discriminator(gen_x))

        real_loss = self.discriminator(x)
        fake_loss = self.discriminator(gen_x.detach())
        gp_term = gradient_penalty(self.discriminator, x, gen_x)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term

        self.val_g_loss.update(g_loss)
        self.val_d_loss.update(d_loss)

        if 'fid' in self.metrics:
            self.val_fid.update(x, real=True)
            self.val_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.val_is.update(gen_x)

        return g_loss, d_loss

    def on_validation_epoch_end(self) -> None:
        self.log("val_g_loss",
                 self.val_g_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)
        self.log("val_d_loss",
                 self.val_d_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)

        if 'fid' in self.metrics:
            self.log("val_fid",
                     self.val_fid.compute(),
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)
        if 'is' in self.metrics:
            self.log("val_is",
                     self.val_is.compute()[0],
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)

        # print the metrics
        str = "\n[VAL] "
        for key, value in self.trainer.logged_metrics.items():
            if key.startswith("val_"):
                str += f"{key}: {value:.3f} "
        print(str + '\n')

        # reset the metrics
        self.val_g_loss.reset()
        self.val_d_loss.reset()

        if 'fid' in self.metrics:
            self.val_fid.reset()
        if 'is' in self.metrics:
            self.val_is.reset()

    @torch.enable_grad()
    def test_step(self, batch: Tuple[torch.Tensor],
                  batch_idx: int) -> torch.Tensor:
        x, _ = batch

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        gen_x = self.generator(latent)
        g_loss = -torch.mean(self.discriminator(gen_x))

        real_loss = self.discriminator(x)
        fake_loss = self.discriminator(gen_x.detach())
        gp_term = gradient_penalty(self.discriminator, x, gen_x)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term

        self.test_g_loss.update(g_loss)
        self.test_d_loss.update(d_loss)

        if 'fid' in self.metrics:
            self.test_fid.update(x, real=True)
            self.test_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.test_is.update(gen_x)

        return g_loss, d_loss

    def on_test_epoch_end(self) -> None:
        self.log("test_g_loss",
                 self.test_g_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)
        self.log("test_d_loss",
                 self.test_d_loss.compute(),
                 prog_bar=False,
                 on_epoch=True,
                 on_step=False,
                 logger=True)

        if 'fid' in self.metrics:
            self.log("test_fid",
                     self.test_fid.compute(),
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)
        if 'is' in self.metrics:
            self.log("test_is",
                     self.test_is.compute()[0],
                     prog_bar=False,
                     on_epoch=True,
                     on_step=False,
                     logger=True)

        # print the metrics
        str = "\n[TEST] "
        for key, value in self.trainer.logged_metrics.items():
            if key.startswith("test_"):
                str += f"{key}: {value:.3f} "
        print(str + '\n')

        # reset the metrics
        self.test_g_loss.reset()
        self.test_d_loss.reset()

        if 'fid' in self.metrics:
            self.test_fid.reset()
        if 'is' in self.metrics:
            self.test_is.reset()

    def configure_optimizers(self):
        generator_optimizer = torch.optim.Adam(self.generator.parameters(),
                                               lr=self.generator_lr,
                                               weight_decay=self.weight_decay)
        discriminator_optimizer = torch.optim.Adam(
            self.discriminator.parameters(),
            lr=self.discriminator_lr,
            weight_decay=self.weight_decay)
        return [generator_optimizer, discriminator_optimizer], []


class CWGANGPTrainer(WGANGPTrainer):
    r'''
    This class provide the implementation for WGAN-GP. It trains a zero-sum game between the generator and the discriminator, just like the traditional generative networks. The generator is optimized to generate simulation samples that are indistinguishable by the discriminator, and the discriminator is optimized to discriminate false samples generated by the generator. Compared with vanilla GAN, with WGAN-GP we can improve the stability of learning, get rid of problems like mode collapse, and provide meaningful learning curves useful for debugging and hyperparameter searches. Thus, existing work typically uses WGAN-GP to generate simulated EEG signals. In particular, the expected labels are additionally provided to guide the discriminator to distinguish whether the sample fits the data distribution of the class. For more details, please refer to the following information.

    - Paper: Gulrajani I, Ahmed F, Arjovsky M, et al. Improved training of wasserstein gans[J]. Advances in neural information processing systems, 2017, 30.
    - URL: https://arxiv.org/abs/1704.00028
    - Related Project: https://github.com/eriklindernoren/PyTorch-GAN

    .. code-block:: python
        
        g_model = BGenerator(in_channels=128)
        d_model = BDiscriminator(in_channels=4)
        trainer = WGANGPTrainer(generator, discriminator)
        trainer.fit(train_loader, val_loader)
        trainer.test(test_loader)

    Args:
        generator (nn.Module): The generator model for EEG signal generation, whose inputs are Gaussian distributed random vectors, outputs are generated EEG signals. The dimensions of the input vector should be defined on the :obj:`in_channel` attribute. The output layer does not need to have a softmax activation function.
        discriminator (nn.Module): The discriminator model to determine whether the EEG signal is real or generated, and the dimension of its output should be equal to the one (i.e., the score to distinguish the real and the fake). The output layer does not need to have a sigmoid activation function.
        generator_lr (float): The learning rate of the generator. (default: :obj:`0.0001`)
        discriminator_lr (float): The learning rate of the discriminator. (default: :obj:`0.0001`)
        weight_gradient_penalty (float): The weight of gradient penalty loss to trade-off between the adversarial training loss and gradient penalty loss. (default: :obj:`1.0`)
        weight_decay: (float): The weight decay (L2 penalty). (default: :obj:`0.0`)
        latent_channels (int): The dimension of the latent vector. If not specified, it will be inferred from the :obj:`in_channels` attribute of the generator. (default: :obj:`None`)
        devices (int): The number of GPUs to use. (default: :obj:`1`)
        accelerator (str): The accelerator to use. Available options are: 'cpu', 'gpu'. (default: :obj:`"cpu"`)
        metrics (List[str]): The metrics to use. The metrics to use. Available options are: 'fid', 'is'. (default: :obj:`[]`)
    
    .. automethod:: fit
    .. automethod:: test
    .. automethod:: sample
    '''

    def training_step(self, batch: Tuple[torch.Tensor],
                      batch_idx: int) -> torch.Tensor:
        x, y = batch
        generator_optimizer, discriminator_optimizer = self.optimizers()

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        # train generator
        self.toggle_optimizer(generator_optimizer)

        gen_x = self.generator(latent, y)
        g_loss = -torch.mean(self.discriminator(gen_x, y))
        g_loss.backward()

        generator_optimizer.step()
        generator_optimizer.zero_grad()
        self.untoggle_optimizer(generator_optimizer)

        # train discriminator
        self.toggle_optimizer(discriminator_optimizer)

        real_loss = self.discriminator(x, y)
        fake_loss = self.discriminator(gen_x.detach(), y)
        gp_term = gradient_penalty(self.discriminator, x, gen_x, y)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term
        d_loss.backward()

        discriminator_optimizer.step()
        discriminator_optimizer.zero_grad()
        self.untoggle_optimizer(discriminator_optimizer)

        self.log("train_g_loss",
                 self.train_g_loss(g_loss),
                 prog_bar=True,
                 on_epoch=False,
                 logger=False,
                 on_step=True)
        self.log("train_d_loss",
                 self.train_d_loss(d_loss),
                 prog_bar=True,
                 on_epoch=False,
                 logger=False,
                 on_step=True)

        if 'fid' in self.metrics:
            self.train_fid.update(x, real=True)
            self.train_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.train_is.update(gen_x)
        # In manual optimization, `training_step` must either return a Tensor or have no return.

    @torch.enable_grad()
    def validation_step(self, batch: Tuple[torch.Tensor],
                        batch_idx: int) -> torch.Tensor:
        x, y = batch

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        gen_x = self.generator(latent, y)
        g_loss = -torch.mean(self.discriminator(gen_x, y))

        real_loss = self.discriminator(x, y)
        fake_loss = self.discriminator(gen_x.detach(), y)
        gp_term = gradient_penalty(self.discriminator, x, gen_x, y)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term

        self.val_g_loss.update(g_loss)
        self.val_d_loss.update(d_loss)

        if 'fid' in self.metrics:
            self.val_fid.update(x, real=True)
            self.val_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.val_is.update(gen_x)

        return g_loss, d_loss

    @torch.enable_grad()
    def test_step(self, batch: Tuple[torch.Tensor],
                  batch_idx: int) -> torch.Tensor:
        x, y = batch

        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)

        gen_x = self.generator(latent, y)
        g_loss = -torch.mean(self.discriminator(gen_x, y))

        real_loss = self.discriminator(x, y)
        fake_loss = self.discriminator(gen_x.detach(), y)
        gp_term = gradient_penalty(self.discriminator, x, gen_x, y)
        d_loss = -torch.mean(real_loss) + torch.mean(
            fake_loss) + self.weight_gradient_penalty * gp_term

        self.test_g_loss.update(g_loss)
        self.test_d_loss.update(d_loss)

        if 'fid' in self.metrics:
            self.test_fid.update(x, real=True)
            self.test_fid.update(gen_x, real=False)

        if 'is' in self.metrics:
            self.test_is.update(gen_x)

        return g_loss, d_loss

    def forward(self, latent: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        return self.generator(latent, y)

    def predict_step(self,
                     batch: Tuple[torch.Tensor],
                     batch_idx: int,
                     dataloader_idx: int = 0):
        x, y = batch
        latent = torch.normal(mean=0,
                              std=1,
                              size=(x.shape[0], self.latent_channels))
        latent = latent.type_as(x)
        return self(latent, y)