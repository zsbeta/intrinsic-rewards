import torch
from torch import nn, Tensor
from typing import Callable, List, Sequence, Tuple
from rainy import Config
from rainy.net import Initializer, make_cnns, NetworkBlock
from rainy.net.prelude import Params
from rainy.utils import Device
from rainy.utils.rms import RunningMeanStdTorch


def _normalize_default(t: Tensor, rms: RunningMeanStdTorch) -> Tensor:
    wh = t.shape[2:]
    t = t[:, 1].reshape(-1, 1, *wh)
    t.sub_(rms.mean.float()).div_(rms.std().float())
    return torch.clamp(t, -5.0, 5.0)


class RewardForwardFilter:
    def __init__(self, gamma: float, nworkers: int, device: Device) -> None:
        self.gamma = gamma
        self.nonepisodic_return = device.zeros(nworkers)

    def update(self, prew: Tensor) -> Tensor:
        self.nonepisodic_return.mul_(self.gamma).add_(prew)
        return self.nonepisodic_return


class IntRewardGenerator:
    def __init__(
            self,
            target: NetworkBlock,
            predictor: NetworkBlock,
            gamma: float,
            nworkers: int,
            device: Device,
            normalizer: Callable[[Tensor, RunningMeanStdTorch], Tensor] = _normalize_default,
    ) -> None:
        target.to(device.unwrapped)
        predictor.to(device.unwrapped)
        self.target = target
        self.predictor = predictor
        self.device = device
        self.ob_rms = RunningMeanStdTorch(target.input_dim[1:], device)
        self.normalizer = normalizer
        self.rff = RewardForwardFilter(gamma, nworkers, device)
        self.rff_rms = RunningMeanStdTorch(shape=(), device=device)
        self.nworkers = nworkers
        self.cached_target = device.ones(0)

    def gen_rewards(self, state: Tensor) -> Tensor:
        s = self.device.tensor(state).mul_(255.0)
        self.ob_rms.update(s.double().view(-1, *self.ob_rms.mean.shape))
        with torch.no_grad():
            normalized_s = self.normalizer(s, self.ob_rms)
            self.cached_target = self.target(normalized_s)
            prediction = self.predictor(normalized_s)
        nsteps = s.size(0) // self.nworkers
        error = (self.cached_target - prediction).pow(2)
        rewards = error.view(nsteps, self.nworkers, -1).mean(dim=-1)
        for i in range(nsteps):
            rew = self.rff.update(rewards[i])
            rewards[i].copy_(rew)
        self.rff_rms.update(rewards.view(-1))
        return rewards.div_(self.rff_rms.var.sqrt())

    def aux_loss(self, state: Tensor, target: Tensor, use_ratio: float) -> Tensor:
        s = self.device.tensor(state).mul_(255.0)
        normalized_s = self.normalizer(s, self.ob_rms)
        prediction = self.predictor(normalized_s)
        mask = torch.empty(s.size(0)).uniform_() < use_ratio
        return (target[mask] - prediction[mask]).pow(2).mean()

    def params(self) -> Params:
        return self.predictor.parameters()


class RndConvBody(NetworkBlock):
    def __init__(
            self,
            cnns: List[nn.Module],
            fcs: List[nn.Module],
            input_dim: Tuple[int, int, int],
            activ1: nn.Module = nn.LeakyReLU(negative_slope=0.2, inplace=True),
            activ2: nn.Module = nn.ReLU(inplace=True),
            init: Initializer = Initializer(nonlinearity='relu'),
    ) -> None:
        super().__init__()
        self.cnns = init.make_list(cnns)
        self.fcs = init.make_list(fcs)
        self._input_dim = input_dim
        self.init = init
        self.activ1 = activ1
        self.activ2 = activ2

    @property
    def input_dim(self) -> Tuple[int, ...]:
        return self._input_dim

    @property
    def output_dim(self) -> int:
        return self.fcs[-1].out_features

    def forward(self, x: Tensor) -> Tensor:
        for cnn in self.cnns:
            x = self.activ1(cnn(x))
        x = x.view(x.size(0), -1)
        for fc in self.fcs[:-1]:
            x = self.activ2(fc(x))
        return self.fcs[-1](x)


def irew_gen_deafult(
        params: Sequence[tuple] = ((8, 4), (4, 2), (3, 1)),
        channels: Sequence[int] = (32, 64, 64),
        output_dim: int = 512,
) -> Callable[[Config, Device], IntRewardGenerator]:
    def _make_irew_gen(cfg: Config, device: Device) -> IntRewardGenerator:
        input_dim = 1, *cfg.state_dim[1:]
        cnns, hidden = make_cnns(input_dim, params, channels)
        target = RndConvBody(cnns, [nn.Linear(hidden, output_dim)], input_dim)
        predictor_fc = [
            nn.Linear(hidden, output_dim),
            nn.Linear(output_dim, output_dim),
            nn.Linear(output_dim, output_dim)
        ]
        cnns, _ = make_cnns(input_dim, params, channels)
        predictor = RndConvBody(cnns, predictor_fc, input_dim)
        return IntRewardGenerator(
            target,
            predictor,
            cfg.int_discount_factor,
            cfg.nworkers,
            device
        )
    return _make_irew_gen
