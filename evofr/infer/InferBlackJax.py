from typing import Callable, Dict, Optional

import jax
from jax import random
from numpyro.infer.util import Predictive, initialize_model

import blackjax

from evofr.data.data_spec import DataSpec
from evofr.infer.backends import Backend
from evofr.models.model_spec import ModelSpec
from evofr.posterior.posterior_handler import PosteriorHandler


class BlackJaxNumpyro:
    @staticmethod
    def init(key, model, data):
        key, subkey = random.split(key)
        init_parms, potential_fn_gen, *_ = initialize_model(
            subkey, model, model_kwargs=data, dynamic_args=True
        )

        def logdensity_fn(position):
            return -potential_fn_gen(**data)(position)

        initial_position = init_parms.z
        return initial_position, logdensity_fn

    @staticmethod
    def predict(key, model, data, samples):
        predictive = Predictive(model, samples)
        key, subkey = random.split(key)
        samples_pred = predictive(subkey, pred=True, **data)
        return {**samples, **samples_pred}


class BlackJaxProvided:
    @staticmethod
    def init(key, model: ModelSpec, data: Dict):
        # Find density function with model_spec
        if hasattr(model, "logdensity_fn_gen"):
            logdensity_fn = model.logdensity_fn_gen(data)
        elif hasattr(model, "logdensity_fn"):
            logdensity_fn = model.logdensity_fn
        else:
            logdensity_fn = lambda _: None
        if hasattr(model, "initial_position"):
            init_position = model.initial_position
        elif hasattr(model, "initial_position_fn"):
            init_position = model.initial_position_fn(key)
        else:
            init_position = None
        return init_position, logdensity_fn

    @staticmethod
    def predict(key, model: ModelSpec, data: Dict, samples: Dict) -> Dict:
        if not hasattr(model, "pred_fn"):
            return samples
        samples_pred = model.pred_fn(key, samples, data)
        return {**samples, **samples_pred}


class BlackJaxHandler:
    def __init__(self, kernel, **kernel_kwargs):
        self.kernel_fn = kernel
        self.kernel_kwargs = kernel_kwargs
        self.seed = 100
        self.rng_key = random.PRNGKey(self.seed)
        self.state = None

    @staticmethod
    def inference_loop(rng_key, kernel, initial_state, num_samples):
        @jax.jit
        def one_step(state, rng_key):
            state, info = kernel(rng_key, state)
            return state, (state, info)

        keys = random.split(rng_key, num_samples)
        _, (states, infos) = jax.lax.scan(one_step, initial_state, keys)
        return states, infos

    @staticmethod
    def _initialize(
        key,
        model: ModelSpec,
        data: Dict,
        backend: Optional[Backend] = None,
    ):
        # Check if model has a backend
        if hasattr(model, "backend") and backend is None:
            backend = model.backend

        # If no backend provided or defined elsewhere
        # default to numpyro
        if backend is None:
            return BlackJaxNumpyro.init(key, model.model_fn, data)

        # Otherwise use provided backend
        if backend == Backend.NUMPYRO:
            return BlackJaxNumpyro.init(key, model.model_fn, data)

        if backend == Backend.PROVIDED:
            return BlackJaxProvided.init(key, model, data)

        return None, lambda _: None

    @staticmethod
    def _predict(
        key,
        model: ModelSpec,
        data: Dict,
        samples: Dict,
        backend: Optional[Backend] = None,
    ):
        # Check if model has a backend
        if hasattr(model, "backend") and backend is None:
            backend = model.backend

        # Otherwise use suggested
        if backend == Backend.NUMPYRO:
            return BlackJaxNumpyro.predict(key, model.model_fn, data, samples)

        if backend == Backend.PROVIDED:
            return BlackJaxProvided.predict(key, model, data, samples)

        # or default to numpyro
        if backend is None:
            return BlackJaxNumpyro.predict(key, model.model_fn, data, samples)
        return dict()

    def run_warmup(
        self, initial_position, logdensity_fn: Callable, num_warmup: int
    ):
        num_warmup = 1 if num_warmup < 1 else num_warmup
        adapt = blackjax.window_adaptation(
            self.kernel_fn,
            logdensity_fn,
            **self.kernel_kwargs,
            num_steps=num_warmup
        )
        self.rng_key, key = random.split(self.rng_key)
        last_state, kernel, _ = adapt.run(key, initial_position)
        return last_state, kernel

    def fit(
        self,
        model: ModelSpec,
        data: Dict,
        num_warmup: int,
        num_samples: int,
    ):
        self.rng_key, key = random.split(self.rng_key)
        initial_position, logdensity_fn = self._initialize(key, model, data)

        # Run adapt window
        if num_warmup > 0:
            starting_state, kernel = self.run_warmup(
                initial_position, logdensity_fn, num_warmup=num_warmup
            )
        else:
            kernel = self.kernel_fn(logdensity_fn, **self.kernel_kwargs)
            starting_state = kernel.init(initial_position)

        # Run sampling
        self.rng_key, key = random.split(self.rng_key)
        self.states, self.infos = self.inference_loop(
            key, kernel, starting_state, num_samples
        )

    @property
    def samples(self) -> Dict:
        if self.states is not None:
            return self.states.position
        return dict()

    def predict(self, model: ModelSpec, data: Dict) -> Dict:
        return self._predict(self.rng_key, model, data, self.samples)


class InferBlackJax:
    def __init__(
        self, num_warmup: int, num_samples: int, kernel, **kernel_kwargs
    ):
        """Construct class for specifying MCMC inference method.

        Parameters
        ----------
        num_warmup:
            number of warmup samples to run.

        num_samples:
            number of samples to return from MCMC.

        kernel:
            transition kernel for MCMC.

        Returns
        -------
        InferBlackJax
        """
        self.num_warmup = num_warmup
        self.num_samples = num_samples
        self.handler = BlackJaxHandler(kernel=kernel, **kernel_kwargs)

    def fit(
        self, model: ModelSpec, data: DataSpec, name: Optional[str] = None
    ) -> PosteriorHandler:
        """Fit model given data using specificed MCMC method.

        Parameters
        ----------
        model:
            ModelSpec for model

        data:
            DataSpec for data to do inference on

        name:
            name used to index posterior

        Returns
        -------
        PosteriorHandler
        """
        # Create and augment data dictionary
        input = data.make_data_dict()
        model.augment_data(input)

        # Fit model and retrieve samples
        self.handler.fit(model, input, self.num_warmup, self.num_samples)
        samples = self.handler.predict(model, input)

        # Create object to hold posterior samples and data
        self.posterior = PosteriorHandler(
            samples=samples, data=data, name=name if name is not None else ""
        )
        return self.posterior
