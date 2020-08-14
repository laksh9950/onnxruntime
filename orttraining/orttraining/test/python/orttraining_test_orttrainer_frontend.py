import inspect
import onnx
import os
import pytest
import torch

from numpy.testing import assert_allclose

from onnxruntime.capi._pybind_state import set_seed
from onnxruntime.capi.ort_trainer import IODescription as Legacy_IODescription,\
                                         ModelDescription as Legacy_ModelDescription,\
                                         LossScaler as Legacy_LossScaler,\
                                         ORTTrainer as Legacy_ORTTrainer
from onnxruntime.capi.training import _utils, amp, optim, orttrainer, TrainStepInfo,\
                                      model_desc_validation as md_val,\
                                      orttrainer_options as orttrainer_options
import _test_helpers


###############################################################################
# Helper functions ############################################################
###############################################################################


def _load_pytorch_transformer_model(device, dynamic_axes=False, legacy_api=False):
    # Loads external Pytorch TransformerModel into utils
    pytorch_transformer_path = os.path.join('..', '..', '..', 'samples', 'python', 'pytorch_transformer')
    pt_model_path = os.path.join(pytorch_transformer_path, 'pt_model.py')
    pt_model = _utils.import_module_from_file(pt_model_path)
    ort_utils_path = os.path.join(pytorch_transformer_path, 'ort_utils.py')
    ort_utils = _utils.import_module_from_file(ort_utils_path)
    utils_path = os.path.join(pytorch_transformer_path, 'utils.py')
    utils = _utils.import_module_from_file(utils_path)

    # Modeling
    model = pt_model.TransformerModel(28785, 200, 2, 200, 2, 0.2).to(device)
    my_loss = ort_utils.my_loss
    if legacy_api:
        if dynamic_axes:
            model_desc = ort_utils.legacy_transformer_model_description_dynamic_axes()
        else:
            model_desc = ort_utils.legacy_transformer_model_description()
    else:
        if dynamic_axes:
            model_desc = ort_utils.transformer_model_description_dynamic_axes()
        else:
            model_desc = ort_utils.transformer_model_description()


    # Preparing data
    train_data, val_data, test_data = utils.prepare_data(device, 20, 20)
    return model, model_desc, my_loss, utils.get_batch, train_data, val_data, test_data


###############################################################################
# Testing starts here #########################################################
###############################################################################


@pytest.mark.parametrize("test_input", [
    ({}),
    ({'batch': {},
      'device': {},
      'distributed': {},
      'mixed_precision': {},
      'utils': {},
      '_internal_use': {}})
])
def testORTTrainerOptionsDefaultValues(test_input):
    ''' Test different ways of using default values for incomplete input'''

    expected_values = {
        'batch': {
            'gradient_accumulation_steps': 1
        },
        'device': {
            'id': 'cuda',
            'mem_limit': 0
        },
        'distributed': {
            'world_rank': 0,
            'world_size': 1,
            'local_rank': 0,
            'allreduce_post_accumulation': False,
            'deepspeed_zero_optimization': {
                'stage' : 0,
            },
            'enable_adasum': False
        },
        'lr_scheduler': None,
        'mixed_precision': {
            'enabled': False,
            'loss_scaler': None
        },
        'utils': {
            'frozen_weights': [],
            'grad_norm_clip': True,
            'invertible_layer_norm_gradient': False,
        },
        'debug': {
            'deterministic_compute': False
        },
        '_internal_use': {
            'enable_internal_postprocess': True,
            'extra_postprocess': None,
            'onnx_opset_version' : 12
        }
    }

    actual_values = orttrainer_options.ORTTrainerOptions(test_input)
    assert actual_values._validated_opts == expected_values


@pytest.mark.parametrize("input,error_msg", [
    ({'mixed_precision': {'enabled': 1}},\
        "Invalid options: {'mixed_precision': [{'enabled': ['must be of boolean type']}]}")
])
def testORTTrainerOptionsInvalidMixedPrecisionEnabledSchema(input, error_msg):
    '''Test an invalid input based on schema validation error message'''

    with pytest.raises(ValueError) as e:
        orttrainer_options.ORTTrainerOptions(input)
    assert str(e.value) == error_msg


@pytest.mark.parametrize("input_dict,input_dtype,output_dtype", [
    ({'inputs': [('in0', [])],
      'outputs': [('out0', []), ('out1', [])]},(torch.int,),(torch.float,torch.int32,)),
    ({'inputs': [('in0', ['batch', 2, 3])],
      'outputs': [('out0', [], True)]}, (torch.int8,), (torch.int16,)),
    ({'inputs': [('in0', []), ('in1', [1]), ('in2', [1, 2]), ('in3', [1000, 'dyn_ax1']), ('in4', ['dyn_ax1', 'dyn_ax2', 'dyn_ax3'])],
      'outputs': [('out0', [], True), ('out1', [1], False), ('out2', [1, 'dyn_ax1', 3])]},
        (torch.float,torch.uint8,torch.bool,torch.double,torch.half,), (torch.float,torch.float,torch.int64))
])
def testORTTrainerModelDescValidSchemas(input_dict, input_dtype, output_dtype):
    r''' Test different ways of using default values for incomplete input'''

    model_description = md_val._ORTTrainerModelDesc(input_dict)

    # Validating hard-coded learning rate description
    assert model_description.learning_rate.name == md_val.LEARNING_RATE_IO_DESCRIPTION_NAME
    assert model_description.learning_rate.shape == [1]
    assert model_description.learning_rate.dtype == torch.float32

    # Validating model description from user
    for idx, i_desc in enumerate(model_description.inputs):
        assert isinstance(i_desc, model_description._InputDescription)
        assert len(i_desc) == 2
        assert input_dict['inputs'][idx][0] == i_desc.name
        assert input_dict['inputs'][idx][1] == i_desc.shape
    for idx, o_desc in enumerate(model_description.outputs):
        assert isinstance(o_desc, model_description._OutputDescription)
        assert len(o_desc) == 3
        assert input_dict['outputs'][idx][0] == o_desc.name
        assert input_dict['outputs'][idx][1] == o_desc.shape
        is_loss = input_dict['outputs'][idx][2] if len(input_dict['outputs'][idx]) == 3 else False
        assert is_loss == o_desc.is_loss

    # Set is_finite name and check its description
    model_description.is_finite = md_val.IS_FINITE_IO_DESCRIPTION_NAME
    assert model_description.is_finite.name == md_val.IS_FINITE_IO_DESCRIPTION_NAME
    assert model_description.is_finite.shape == [1]
    assert model_description.is_finite.dtype == torch.bool

    # Set loss_scale_input and check its description
    model_description.loss_scale_input = md_val.LOSS_SCALE_INPUT_IO_DESCRIPTION_NAME
    assert model_description.loss_scale_input.name == md_val.LOSS_SCALE_INPUT_IO_DESCRIPTION_NAME
    assert model_description.loss_scale_input.shape == []
    assert model_description.loss_scale_input.dtype == torch.float32

    # Append type to inputs/outputs tuples
    for idx, i_desc in enumerate(model_description.inputs):
        model_description.add_type_to_input_description(idx, input_dtype[idx])
    for idx, o_desc in enumerate(model_description.outputs):
        model_description.add_type_to_output_description(idx, output_dtype[idx])

    # Verify inputs/outputs tuples are replaced by the typed counterparts
    for idx, i_desc in enumerate(model_description.inputs):
        assert isinstance(i_desc, model_description._InputDescriptionTyped)
        assert input_dtype[idx] == i_desc.dtype
    for idx, o_desc in enumerate(model_description.outputs):
        assert isinstance(o_desc, model_description._OutputDescriptionTyped)
        assert output_dtype[idx] == o_desc.dtype


@pytest.mark.parametrize("input_dict,error_msg", [
    ({'inputs': [(True, [])],
      'outputs': [(True, [])]},
      "Invalid model_desc: {'inputs': [{0: ['the first element of the tuple (aka name) must be a string']}], "
                           "'outputs': [{0: ['the first element of the tuple (aka name) must be a string']}]}"),
    ({'inputs': [('in1', None)],
      'outputs': [('out1', None)]},
      "Invalid model_desc: {'inputs': [{0: ['the second element of the tuple (aka shape) must be a list']}], "
                           "'outputs': [{0: ['the second element of the tuple (aka shape) must be a list']}]}"),
    ({'inputs': [('in1', [])],
     'outputs': [('out1', [], None)]},
     "Invalid model_desc: {'outputs': [{0: ['the third element of the tuple (aka is_loss) must be a boolean']}]}"),
    ({'inputs': [('in1', [True])],
      'outputs': [('out1', [True])]},
      "Invalid model_desc: {'inputs': [{0: ['each shape must be either a string or integer']}], "
                           "'outputs': [{0: ['each shape must be either a string or integer']}]}"),
    ({'inputs': [('in1', [])],
      'outputs': [('out1', [], True), ('out2', [], True)]},
      "Invalid model_desc: {'outputs': [{1: ['only one is_loss can bet set to True']}]}"),
    ({'inputz': [('in1', [])],
      'outputs': [('out1', [], True)]},
      "Invalid model_desc: {'inputs': ['required field'], 'inputz': ['unknown field']}"),
    ({'inputs': [('in1', [])],
      'outputz': [('out1', [], True)]},
      "Invalid model_desc: {'outputs': ['required field'], 'outputz': ['unknown field']}"),
])
def testORTTrainerModelDescInvalidSchemas(input_dict, error_msg):
    r''' Test different ways of using default values for incomplete input'''
    with pytest.raises(ValueError) as e:
        md_val._ORTTrainerModelDesc(input_dict)
    assert str(e.value) == error_msg


def testDynamicLossScaler():
    rtol = 1e-5
    default_scaler = amp.loss_scaler.DynamicLossScaler()

    # Initial state
    train_step_info = orttrainer.TrainStepInfo(optim.LambConfig())
    assert_allclose(default_scaler.loss_scale, float(1 << 16),
                    rtol=rtol, err_msg="loss scale mismatch")
    assert default_scaler.up_scale_window == 2000
    assert_allclose(default_scaler.min_loss_scale, 1.0,
                    rtol=rtol, err_msg="min loss scale mismatch")
    assert_allclose(default_scaler.max_loss_scale, float(
        1 << 24), rtol=rtol, err_msg="max loss scale mismatch")

    # Performing 9*2000 updates to cover all branches of LossScaler.update(train_step_info.all_finite=True)
    loss_scale = float(1 << 16)
    for cycles in range(1, 10):

        # 1999 updates without overflow produces 1999 stable steps
        for i in range(1, 2000):
            new_loss_scale = default_scaler.update(train_step_info)
            assert default_scaler._stable_steps_count == i
            assert_allclose(new_loss_scale, loss_scale,
                            rtol=rtol, err_msg=f"loss scale mismatch at update {i}")

        # 2000th update without overflow doubles the loss and zero stable steps until max_loss_scale is reached
        new_loss_scale = default_scaler.update(train_step_info)
        if cycles <= 8:
            loss_scale *= 2
        assert default_scaler._stable_steps_count == 0
        assert_allclose(new_loss_scale, loss_scale,
                        rtol=rtol, err_msg="loss scale mismatch")

    # After 8 cycles, loss scale should be float(1 << 16)*(2**8)
    assert_allclose(new_loss_scale, float(1 << 16)
                    * (2**8), rtol=rtol, err_msg="loss scale mismatch")

    # After 9 cycles, loss scale reaches max_loss_scale and it is not doubled from that point on
    loss_scale = float(1 << 16)*(2**8)
    for count in range(1, 2050):
        new_loss_scale = default_scaler.update(train_step_info)
        assert default_scaler._stable_steps_count == (count % 2000)
        assert_allclose(new_loss_scale, loss_scale,
                        rtol=rtol, err_msg="loss scale mismatch")

    # Setting train_step_info.all_finite = False to test down scaling
    train_step_info.all_finite = False

    # Performing 24 updates to half the loss scale each time
    loss_scale = float(1 << 16)*(2**8)
    for count in range(1, 25):
        new_loss_scale = default_scaler.update(train_step_info)
        loss_scale /= 2
        assert default_scaler._stable_steps_count == 0
        assert_allclose(new_loss_scale, loss_scale,
                        rtol=rtol, err_msg="loss scale mismatch")

    # After 24 updates with gradient overflow, loss scale is 1.0
    assert_allclose(new_loss_scale, 1.,
                    rtol=rtol, err_msg="loss scale mismatch")

    # After 25 updates, min_loss_scale is reached and loss scale is not halfed from that point on
    for count in range(1, 5):
        new_loss_scale = default_scaler.update(train_step_info)
        assert default_scaler._stable_steps_count == 0
        assert_allclose(new_loss_scale, loss_scale,
                        rtol=rtol, err_msg="loss scale mismatch")


def testDynamicLossScalerCustomValues():
    rtol = 1e-5
    scaler = amp.loss_scaler.DynamicLossScaler(automatic_update=False,
                                               loss_scale=3,
                                               up_scale_window=7,
                                               min_loss_scale=5,
                                               max_loss_scale=10)
    assert scaler.automatic_update == False
    assert_allclose(scaler.loss_scale, 3, rtol=rtol,
                    err_msg="loss scale mismatch")
    assert_allclose(scaler.min_loss_scale, 5, rtol=rtol,
                    err_msg="min loss scale mismatch")
    assert_allclose(scaler.max_loss_scale, 10, rtol=rtol,
                    err_msg="max loss scale mismatch")
    assert scaler.up_scale_window == 7


def testTrainStepInfo():
    '''Test valid initializations of TrainStepInfo'''

    optimizer_config = optim.LambConfig()
    fetches=['out1','out2']
    step_info = orttrainer.TrainStepInfo(optimizer_config=optimizer_config,
                                         all_finite=False,
                                         fetches=fetches,
                                         optimization_step=123,
                                         step=456)
    assert step_info.optimizer_config == optimizer_config
    assert step_info.all_finite == False
    assert step_info.fetches == fetches
    assert step_info.optimization_step == 123
    assert step_info.step == 456

    step_info = orttrainer.TrainStepInfo(optimizer_config)
    assert step_info.optimizer_config == optimizer_config
    assert step_info.all_finite == True
    assert step_info.fetches == []
    assert step_info.optimization_step == 0
    assert step_info.step == 0


@pytest.mark.parametrize("invalid_input", [
    (-1),
    ('Hello'),
])
def testTrainStepInfoInvalidInput(invalid_input):
    '''Test invalid initialization of TrainStepInfo'''
    optimizer_config = optim.LambConfig()
    with pytest.raises(AssertionError):
        orttrainer.TrainStepInfo(optimizer_config=invalid_input)

    with pytest.raises(AssertionError):
        orttrainer.TrainStepInfo(optimizer_config, all_finite=invalid_input)

    with pytest.raises(AssertionError):
        orttrainer.TrainStepInfo(optimizer_config, fetches=invalid_input)

    with pytest.raises(AssertionError):
        orttrainer.TrainStepInfo(optimizer_config, optimization_step=invalid_input)

    with pytest.raises(AssertionError):
        orttrainer.TrainStepInfo(optimizer_config, step=invalid_input)


@pytest.mark.parametrize("optim_name,lr,alpha,default_alpha", [
    ('AdamOptimizer', .1, .2, None),
    ('LambOptimizer', .2, .3, None),
    ('SGDOptimizer', .3, .4, None),
    ('SGDOptimizer', .3, .4, .5)
])
def testOptimizerConfig(optim_name, lr, alpha, default_alpha):
    '''Test initialization of _OptimizerConfig'''
    defaults = {'lr': lr, 'alpha': alpha}
    params = [{'params': ['fc1.weight', 'fc2.weight']}]
    if default_alpha is not None:
        params[0].update({'alpha': default_alpha})
    else:
        params[0].update({'alpha': alpha})
    cfg = optim.config._OptimizerConfig(
        name=optim_name, params=params, defaults=defaults)

    assert cfg.name == optim_name
    rtol = 1e-03
    assert_allclose(defaults['lr'],
                    cfg.lr, rtol=rtol, err_msg="lr mismatch")

    # 1:1 mapping between defaults and params's hyper parameters
    for param in params:
        for k, _ in param.items():
            if k != 'params':
                assert k in cfg.defaults, "hyper parameter {k} not present in one of the parameter params"
    for k, _ in cfg.defaults.items():
        for param in cfg.params:
            assert k in param, "hyper parameter {k} not present in one of the parameter params"


@pytest.mark.parametrize("optim_name,defaults,params", [
    ('AdamOptimizer', {'lr': -1}, []),  # invalid lr
    ('FooOptimizer', {'lr': 0.001}, []),  # invalid name
    ('SGDOptimizer', [], []),  # invalid type(defaults)
    (optim.AdamConfig, {'lr': 0.003}, []),  # invalid type(name)
    ('AdamOptimizer', {'lr': None}, []),  # missing 'lr' hyper parameter
    ('SGDOptimizer', {'lr': 0.004}, {}),  # invalid type(params)
    # invalid type(params[i])
    ('AdamOptimizer', {'lr': 0.005, 'alpha': 2}, [[]]),
    # missing 'params' at 'params'
    ('AdamOptimizer', {'lr': 0.005, 'alpha': 2}, [{'alpha': 1}]),
    # missing 'alpha' at 'defaults'
    ('AdamOptimizer', {'lr': 0.005}, [{'params': 'param1', 'alpha': 1}]),
])
def testOptimizerConfigInvalidInputs(optim_name, defaults, params):
    '''Test invalid initialization of _OptimizerConfig'''

    with pytest.raises(AssertionError):
        optim.config._OptimizerConfig(
            name=optim_name, params=params, defaults=defaults)


def testSGDConfig():
    '''Test initialization of SGD'''
    cfg = optim.SGDConfig()
    assert cfg.name == 'SGDOptimizer'

    rtol = 1e-05
    assert_allclose(0.001, cfg.lr, rtol=rtol, err_msg="lr mismatch")

    cfg = optim.SGDConfig(lr=0.002)
    assert_allclose(0.002, cfg.lr, rtol=rtol, err_msg="lr mismatch")

    # SGD does not support params
    with pytest.raises(AssertionError) as e:
        params = [{'params': ['layer1.weight'], 'lr': 0.1}]
        optim.SGDConfig(params=params, lr=0.002)
        assert_allclose(0.002, cfg.lr, rtol=rtol, err_msg="lr mismatch")
    assert str(e.value) == "'params' must be an empty list for SGD optimizer"


def testAdamConfig():
    '''Test initialization of Adam'''
    cfg = optim.AdamConfig()
    assert cfg.name == 'AdamOptimizer'

    rtol = 1e-05
    assert_allclose(0.001, cfg.lr, rtol=rtol, err_msg="lr mismatch")
    assert_allclose(0.9, cfg.alpha, rtol=rtol, err_msg="alpha mismatch")
    assert_allclose(0.999, cfg.beta, rtol=rtol, err_msg="beta mismatch")
    assert_allclose(0.0, cfg.lambda_coef, rtol=rtol,
                    err_msg="lambda_coef mismatch")
    assert_allclose(1e-8, cfg.epsilon, rtol=rtol, err_msg="epsilon mismatch")
    assert cfg.do_bias_correction == True, "lambda_coef mismatch"
    assert cfg.weight_decay_mode == optim.AdamConfig.DecayMode.BEFORE_WEIGHT_UPDATE, "weight_decay_mode mismatch"


def testLambConfig():
    '''Test initialization of Lamb'''
    cfg = optim.LambConfig()
    assert cfg.name == 'LambOptimizer'
    rtol = 1e-05
    assert_allclose(0.001, cfg.lr, rtol=rtol, err_msg="lr mismatch")
    assert_allclose(0.9, cfg.alpha, rtol=rtol, err_msg="alpha mismatch")
    assert_allclose(0.999, cfg.beta, rtol=rtol, err_msg="beta mismatch")
    assert_allclose(0.0, cfg.lambda_coef, rtol=rtol,
                    err_msg="lambda_coef mismatch")
    assert cfg.ratio_min == float('-inf'), "ratio_min mismatch"
    assert cfg.ratio_max == float('inf'), "ratio_max mismatch"
    assert_allclose(1e-6, cfg.epsilon, rtol=rtol, err_msg="epsilon mismatch")
    assert cfg.do_bias_correction == True, "lambda_coef mismatch"


@pytest.mark.parametrize("optim_name", [
    ('Adam'),
    ('Lamb')
])
def testParamparams(optim_name):
    rtol = 1e-5
    params = [{'params': ['layer1.weight'], 'alpha': 0.1}]
    if optim_name == 'Adam':
        cfg = optim.AdamConfig(params=params, alpha=0.2)
    elif optim_name == 'Lamb':
        cfg = optim.LambConfig(params=params, alpha=0.2)
    else:
        raise ValueError('invalid input')
    assert len(cfg.params) == 1, "params should have length 1"
    assert_allclose(cfg.params[0]['alpha'], 0.1,
                    rtol=rtol, err_msg="invalid lr on params[0]")


@pytest.mark.parametrize("optim_name", [
    ('Adam'),
    ('Lamb')
])
def testInvalidParamparams(optim_name):
    # lr is not supported within params
    with pytest.raises(AssertionError) as e:
        params = [{'params': ['layer1.weight'], 'lr': 0.1}]
        if optim_name == 'Adam':
            optim.AdamConfig(params=params, lr=0.2)
        elif optim_name == 'Lamb':
            optim.LambConfig(params=params, lr=0.2)
        else:
            raise ValueError('invalid input')
    assert str(e.value) == "'lr' is not supported inside params"


def testLinearLRSchedulerCreation():
    total_steps = 10
    warmup = 0.05

    lr_scheduler = optim.lr_scheduler.LinearWarmupLRScheduler(total_steps,
                                                              warmup)

    # Initial state
    assert lr_scheduler.total_steps == total_steps
    assert lr_scheduler.warmup == warmup


@pytest.mark.parametrize("lr_scheduler,expected_values", [
    (optim.lr_scheduler.ConstantWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                    0.023843, 0.023843, 0.023843, 0.023843, 0.023843]),
    (optim.lr_scheduler.CosineWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                  0.010225, 0.002989, 0.0005158, 0.000040937, 0.0000008291]),
    (optim.lr_scheduler.LinearWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                  0.021675, 0.0157636, 0.0085983, 0.0031266, 0.00056847]),
    (optim.lr_scheduler.PolyWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                0.0160749, 0.0096935, 0.0050622, 0.0021585, 0.000650833])
])
def testLRSchedulerUpdateImpl(lr_scheduler, expected_values):
    # Test tolerance
    rtol = 1e-04

    # Initial state
    initial_lr = 1
    total_steps = 10
    warmup = 0.5
    optimizer_config = optim.SGDConfig(lr=initial_lr)
    lr_scheduler = lr_scheduler(total_steps, warmup)

    # First half is warmup
    for optimization_step in range(total_steps):
        # Emulate ORTTRainer.train_step() call that updates its train_step_info
        train_step_info = TrainStepInfo(optimizer_config=optimizer_config, optimization_step=optimization_step)

        lr_scheduler.step(train_step_info)
        lr_list = lr_scheduler.get_last_lr()
        assert len(lr_list) == 1
        assert_allclose(lr_list[0],
                        expected_values[optimization_step], rtol=rtol, err_msg="lr mismatch")


@pytest.mark.parametrize("step_fn, lr_scheduler, expected_lr_values, device", [
    ('train_step', None, None, 'cuda'),
    ('eval_step', None, None, 'cpu'),
    ('train_step', optim.lr_scheduler.ConstantWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                    0.023843, 0.023843, 0.023843, 0.023843, 0.023843], 'cpu'),
    ('train_step', optim.lr_scheduler.CosineWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                  0.010225, 0.002989, 0.0005158, 0.000040937, 0.0000008291], 'cuda'),
    ('train_step', optim.lr_scheduler.LinearWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                  0.021675, 0.0157636, 0.0085983, 0.0031266, 0.00056847], 'cpu'),
    ('train_step', optim.lr_scheduler.PolyWarmupLRScheduler, [0.181818, 0.066116, 0.036063, 0.026228, 0.023843,
                                                0.0160749, 0.0096935, 0.0050622, 0.0021585, 0.000650833], 'cuda')
])
def testInstantiateORTTrainer(step_fn, lr_scheduler, expected_lr_values, device):
    total_steps = 1
    initial_lr = 1.
    tolerance = 1e-4

    # PyTorch Transformer model as example
    opts = {'device' : {'id' : device}}
    if lr_scheduler:
        total_steps = 10
        opts.update({'lr_scheduler' : lr_scheduler(total_steps=total_steps, warmup=0.5)})
    opts = orttrainer.ORTTrainerOptions(opts)
    optim_config = optim.LambConfig(lr=initial_lr)
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=opts)

    # Run a train or evaluation step
    if step_fn == 'eval_step':
        data, targets = batcher_fn(val_data, 0)
    elif step_fn == 'train_step':
        data, targets = batcher_fn(train_data, 0)
    else:
        raise ValueError('Invalid step_fn')

    # Export model to ONNX
    if step_fn == 'eval_step':
        step_fn = trainer.eval_step
        output = trainer.eval_step(data, targets)
    elif step_fn == 'train_step':
        step_fn = trainer.train_step
        for i in range(total_steps):
            output = trainer.train_step(data, targets)
            if lr_scheduler:
                lr_list = trainer.options.lr_scheduler.get_last_lr()
                assert_allclose(lr_list[0], expected_lr_values[i], rtol=tolerance, err_msg="lr mismatch")
    else:
        raise ValueError('Invalid step_fn')
    assert trainer._onnx_model is not None

    # Check output shape after train/eval step
    for out, desc in zip(output, trainer.model_desc.outputs):
        if trainer.loss_fn and desc.is_loss:
            continue
        assert list(out.size()) == desc.shape

    # Check name, shape and dtype of the first len(forward.parameters) ORT graph inputs
    sig = inspect.signature(model.forward)
    for i in range(len(sig.parameters.keys())):
        input_name = trainer.model_desc.inputs[i][0]
        input_dim = trainer.model_desc.inputs[i][1]
        input_type = trainer.model_desc.inputs[i][2]

        assert trainer._onnx_model.graph.input[i].name == input_name
        for dim_idx, dim in enumerate(trainer._onnx_model.graph.input[i].type.tensor_type.shape.dim):
            assert input_dim[dim_idx] == dim.dim_value
            assert input_type == _utils.dtype_onnx_to_torch(
                trainer._onnx_model.graph.input[i].type.tensor_type.elem_type)

    # Check name, shape and dtype of the ORT graph outputs
    for i in range(len(trainer.model_desc.outputs)):
        output_name = trainer.model_desc.outputs[i][0]
        output_dim = trainer.model_desc.outputs[i][1]
        output_type = trainer.model_desc.outputs[i][3]

        assert trainer._onnx_model.graph.output[i].name == output_name
        for dim_idx, dim in enumerate(trainer._onnx_model.graph.output[i].type.tensor_type.shape.dim):
            assert output_dim[dim_idx] == dim.dim_value
            assert output_type == _utils.dtype_onnx_to_torch(
                trainer._onnx_model.graph.output[i].type.tensor_type.elem_type)

    # Save current model as ONNX as a file
    file_name = os.path.join('..','..','..','temp_onnx_model.onnx')
    trainer.save_as_onnx(file_name)
    assert os.path.exists(file_name)
    with open(file_name, "rb") as f:
        bin_str = f.read()
        reload_onnx_model = onnx.load_model_from_string(bin_str)
    os.remove(file_name)

    # Create a new trainer from persisted ONNX model and compare with original ONNX model
    trainer_from_onnx = orttrainer.ORTTrainer(reload_onnx_model, model_desc, optim_config)
    step_fn(data, targets)
    assert trainer_from_onnx._onnx_model is not None
    assert (id(trainer_from_onnx._onnx_model) != id(trainer._onnx_model))
    assert (trainer_from_onnx._onnx_model == trainer._onnx_model)
    assert (trainer_from_onnx._onnx_model.graph == trainer._onnx_model.graph)
    assert (onnx.helper.printable_graph(trainer_from_onnx._onnx_model.graph) == onnx.helper.printable_graph(trainer._onnx_model.graph))


@pytest.mark.parametrize("seed, device", [
    (0, 'cpu'),
    (24, 'cuda')
])
def testORTDeterministicCompute(seed, device):
    # Common setup
    optim_config = optim.LambConfig()
    opts = orttrainer.ORTTrainerOptions({
        'debug' : {
            'deterministic_compute': True
        },
        'device' : {
            'id' : device,
            'mem_limit' : 10*1024*1024
        }
    })

    # Setup for the first ORTTRainer run
    torch.manual_seed(seed)
    set_seed(seed)
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    first_trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=opts)
    data, targets = batcher_fn(train_data, 0)
    _ = first_trainer.train_step(data, targets)
    assert first_trainer._onnx_model is not None
    
    # Setup for the second ORTTRainer run
    torch.manual_seed(seed)
    set_seed(seed)
    model, _, _, _, _, _, _ = _load_pytorch_transformer_model(device)
    second_trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=opts)
    _ = second_trainer.train_step(data, targets)
    assert second_trainer._onnx_model is not None

    # Compare two different instances with identical setup
    assert id(first_trainer._onnx_model) != id(second_trainer._onnx_model)
    _test_helpers.assert_onnx_weights(first_trainer, second_trainer)


@pytest.mark.parametrize("seed,device,expected_loss,fetches", [
    (321, 'cuda', [10.5774, 10.4403, 10.4175, 10.2886, 10.2760], False),
    (321, 'cuda', [10.5774, 10.4403, 10.4175, 10.2886, 10.2760], True),
])
def testORTTrainerMixedPrecisionLossScaler(seed, device, expected_loss, fetches):
    total_steps = len(expected_loss)
    torch.manual_seed(seed)
    set_seed(seed)
    bptt=35

    # Setup ORTTrainer
    loss_scaler = amp.DynamicLossScaler()
    options = orttrainer.ORTTrainerOptions({'device' : {'id' : device},
                                            'mixed_precision' : {
                                                'enabled' : True,
                                                'loss_scaler' : loss_scaler},
                                            'debug' : {'deterministic_compute' : True}})
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)

    # Training loop
    actual_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        if fetches:
            trainer._train_step_info.fetches=['loss']
            loss = trainer.train_step(data, targets)
        else:
            loss, _ = trainer.train_step(data, targets)
        actual_loss.append(loss.cpu())

    # Eval once just to test fetches in action
    val_data, val_targets = batcher_fn(val_data, 0)
    if fetches:
        trainer._train_step_info.fetches=['loss']
        loss = trainer.eval_step(val_data, val_targets)
        trainer._train_step_info.fetches=[]
    loss, preds = trainer.eval_step(val_data, val_targets)

    # Compare loss to ground truth computed from current ORTTrainer API
    _test_helpers.assert_model_outputs(expected_loss, actual_loss, True, rtol=1e-4)
    assert trainer._onnx_model is not None


@pytest.mark.parametrize("seed,device,gradient_accumulation_steps,total_steps,expected_loss", [
    (0, 'cuda', 1, 12, [10.5368022919, 10.4146203995, 10.3635568619, 10.2650547028, 10.2284049988, 10.1304626465,\
        10.0853414536, 9.9987659454, 9.9472427368, 9.8832416534, 9.8223171234, 9.8222122192]),
    (42, 'cuda', 3, 12, [10.6455879211, 10.6247081757, 10.6361322403, 10.5187482834, 10.5345087051, 10.5487670898,\
        10.4833698273, 10.4600019455, 10.4535751343, 10.3774127960, 10.4144191742, 10.3757553101]),
    (123, 'cuda', 7, 12, [10.5353469849, 10.5261383057, 10.5240392685, 10.5013713837, 10.5678377151, 10.5452117920,\
        10.5184345245, 10.4271221161, 10.4458627701, 10.4864749908, 10.4416503906, 10.4467563629]),
    (321, 'cuda', 12, 12, [10.5773944855, 10.5428829193, 10.5974750519, 10.5416746140, 10.6009902954, 10.5684127808,\
        10.5759754181, 10.5636739731, 10.5613927841, 10.5825119019, 10.6031589508, 10.6199369431]),
])
def testORTTrainerGradientAccumulation(seed, device, gradient_accumulation_steps, total_steps, expected_loss):
    torch.manual_seed(seed)
    set_seed(seed)

    # Setup ORTTrainer
    options = orttrainer.ORTTrainerOptions({'device' : {'id' : device},
                                            'batch' : {'gradient_accumulation_steps' : gradient_accumulation_steps},
                                            'debug' : {'deterministic_compute' : True}})
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)

    # Training loop
    actual_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        loss, _ = trainer.train_step(data, targets)
        actual_loss.append(loss.cpu())

    # Compare legacy vs experimental APIs
    _test_helpers.assert_model_outputs(expected_loss, actual_loss, rtol=1e-6)


@pytest.mark.parametrize("dynamic_axes", [
    (True),
    (False),
])
def testORTTrainerDynamicShape(dynamic_axes):
    # Common setup
    device = 'cuda'

    # Setup ORTTrainer
    options = orttrainer.ORTTrainerOptions({})
    model, model_desc, my_loss, batcher_fn,\
        train_data, val_data, _ = _load_pytorch_transformer_model(device, dynamic_axes=dynamic_axes)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)

    # Training loop
    total_steps = 10
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        _, _ = trainer.train_step(data, targets)

    assert trainer._onnx_model is not None


@pytest.mark.parametrize("model_params", [
    (['decoder.weight',
      'transformer_encoder.layers.0.linear1.bias',
      'transformer_encoder.layers.0.linear2.weight',
      'transformer_encoder.layers.1.self_attn.out_proj.weight',
      'transformer_encoder.layers.1.self_attn.out_proj.bias']),
])
def testORTTrainerFrozenWeights(model_params):
    # Common setup
    device = 'cuda'
    total_steps = 10

    # Setup ORTTrainer WITHOUT frozen weights
    options = orttrainer.ORTTrainerOptions({})
    model, model_desc, my_loss, batcher_fn,\
        train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        _, _ = trainer.train_step(data, targets)

    # All model_params must be in the session state
    assert trainer._onnx_model is not None
    session_state = trainer._training_session.get_state()
    assert all([param in session_state for param in model_params])


    # Setup ORTTrainer WITH frozen weights
    options = orttrainer.ORTTrainerOptions({'utils' : {'frozen_weights' : model_params}})
    model, _, _, _, _, _, _ = _load_pytorch_transformer_model(device)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        _, _ = trainer.train_step(data, targets)

    # All model_params CANNOT be in the session state
    assert trainer._onnx_model is not None
    session_state = trainer._training_session.get_state()
    assert not all([param in session_state for param in model_params])


@pytest.mark.parametrize("optim_params", [
    ([{'params' : ['decoder.weight'], }]),
])
def testORTTrainerOptimizerConfigParamGroups(optim_params):
    # Common setup
    device = 'cuda'
    total_steps = 10
    options = orttrainer.ORTTrainerOptions({})

    # Setup ORTTrainer WITHOUT frozen weights
    model, model_desc, my_loss, batcher_fn,\
        train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        _, _ = trainer.train_step(data, targets)

    # All model_params must be in the session state
    assert trainer._onnx_model is not None
    # session_state = trainer._training_session.get_state()
    # assert all([param in session_state for param in model_params])


    # # Setup ORTTrainer WITH frozen weights
    # model, _, _, _, _, _, _ = _load_pytorch_transformer_model(device)
    # trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    # for i in range(total_steps):
    #     data, targets = batcher_fn(train_data, i)
    #     _, _ = trainer.train_step(data, targets)

    # # All model_params CANNOT be in the session state
    # assert trainer._onnx_model is not None
    # session_state = trainer._training_session.get_state()
    # assert not all([param in session_state for param in model_params])


###############################################################################
# Temporary tests comparing Legacy vs Experimental ORTTrainer APIs ############
###############################################################################


@pytest.mark.parametrize("seed,device", [
    (1234, 'cuda')
])
def testORTTrainerLegacyAndExperimentalBasicTraining(seed, device):
    # Common data
    total_steps = 10

    # Setup for the experimental ORTTRainer run
    torch.manual_seed(seed)
    set_seed(seed)
    optim_config = optim.LambConfig()
    opts = orttrainer.ORTTrainerOptions({
        'device' : {
            'id' : device
        },
        'debug' : {
            'deterministic_compute': True
        },
    })
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=opts)
    experimental_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        exp_loss, _ = trainer.train_step(data, targets)
        experimental_loss.append(exp_loss.cpu())

    # Setup for the legacy ORTTrainer run
    torch.manual_seed(seed)
    set_seed(seed)
    model, (model_desc, lr_desc), _, _, _, _, _ = _load_pytorch_transformer_model(device, legacy_api=True)
    legacy_trainer = Legacy_ORTTrainer(model, my_loss, model_desc, "LambOptimizer", None, lr_desc,
                                       device, _use_deterministic_compute=True)
    legacy_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        leg_loss, _ = legacy_trainer.train_step(data, targets, torch.tensor([optim_config.lr]))
        legacy_loss.append(leg_loss.cpu())

    # Compare legacy vs experimental APIs
    _test_helpers.assert_legacy_onnx_weights(trainer, legacy_trainer, rtol=1e-4)
    _test_helpers.assert_model_outputs(legacy_loss, experimental_loss, rtol=1e-6)

@pytest.mark.parametrize("seed,device", [
    (321, 'cuda'),
])
def testORTTrainerLegacyAndExperimentalPrecisionLossScaler(seed, device):
    # Common data
    total_steps = 5

    # Setup experimental API
    torch.manual_seed(seed)
    set_seed(seed)
    loss_scaler = amp.DynamicLossScaler()
    options = orttrainer.ORTTrainerOptions({'device' : {'id' : device},
                                            'mixed_precision' : {
                                                'enabled' : True,
                                                'loss_scaler' : loss_scaler},
                                            'debug' : {'deterministic_compute' : True,}})
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    # Training loop
    experimental_loss = []
    experimental_preds_dtype = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        exp_loss, exp_preds = trainer.train_step(data, targets)
        experimental_loss.append(exp_loss.cpu())
        experimental_preds_dtype.append(exp_preds.dtype)

    # Setup legacy API
    torch.manual_seed(seed)
    set_seed(seed)
    model, (model_desc, lr_desc), _, _, _, _, _ = _load_pytorch_transformer_model(device, legacy_api=True)
    loss_scaler = Legacy_LossScaler('ort_test_input_loss_scalar', True)
    legacy_trainer = Legacy_ORTTrainer(model, my_loss, model_desc, "LambOptimizer",
                                       None, lr_desc, device=device,
                                       _use_deterministic_compute=True,
                                       use_mixed_precision=True,
                                       loss_scaler=loss_scaler)
    # Training loop
    legacy_loss = []
    legacy_preds_dtype = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        leg_loss, leg_preds = legacy_trainer.train_step(data, targets, torch.tensor([optim_config.lr]))
        legacy_loss.append(leg_loss.cpu())
        legacy_preds_dtype.append(leg_preds.dtype)

    # Compare legacy vs experimental APIs
    assert experimental_preds_dtype == legacy_preds_dtype
    _test_helpers.assert_legacy_onnx_weights(trainer, legacy_trainer, rtol=1e-4, atol=1e-2)
    _test_helpers.assert_model_outputs(legacy_loss, experimental_loss, rtol=1e-4)


@pytest.mark.parametrize("seed,device,gradient_accumulation_steps,total_steps", [
    (0, 'cuda', 1, 12),
    (42, 'cuda', 3, 12),
    (123, 'cuda', 7, 12),
    (321, 'cuda', 12, 12),
])
def testORTTrainerLegacyAndExperimentalGradientAccumulation(seed, device, gradient_accumulation_steps, total_steps):
    # Common data
    torch.set_printoptions(precision=10)

    # Setup experimental API
    torch.manual_seed(seed)
    set_seed(seed)
    options = orttrainer.ORTTrainerOptions({'device' : {'id' : device},
                                            'batch' : {'gradient_accumulation_steps' : gradient_accumulation_steps},
                                            'debug' : {'deterministic_compute' : True}})
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    optim_config = optim.LambConfig(lr=0.001)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    # Training loop
    experimental_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        exp_loss, exp_preds = trainer.train_step(data, targets)
        experimental_loss.append(exp_loss.cpu())

    # Setup legacy API
    torch.manual_seed(seed)
    set_seed(seed)
    model, (model_desc, lr_desc), _, _, _, _, _ = _load_pytorch_transformer_model(device, legacy_api=True)
    legacy_trainer = Legacy_ORTTrainer(model, my_loss, model_desc, "LambOptimizer",
                                       None, lr_desc, device=device,
                                       _use_deterministic_compute=True,
                                       gradient_accumulation_steps=gradient_accumulation_steps)
    # Training loop
    legacy_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        leg_loss, leg_preds = legacy_trainer.train_step(data, targets, torch.tensor([optim_config.lr]))
        legacy_loss.append(leg_loss.cpu())

    # Compare legacy vs experimental APIs
    _test_helpers.assert_model_outputs(legacy_loss, experimental_loss, rtol=1e-6)


@pytest.mark.parametrize("seed,device,total_steps", [
    (0, 'cuda', 10),
])
def testORTTrainerLegacyAndExperimentalOptimizerParamGroups(seed, device, total_steps):
    # Common data
    torch.set_printoptions(precision=10)

    # Setup experimental API
    torch.manual_seed(seed)
    set_seed(seed)
    options = orttrainer.ORTTrainerOptions({'device' : {'id' : device},
                                            'debug' : {'deterministic_compute' : True}})
    model, model_desc, my_loss, batcher_fn, train_data, val_data, _ = _load_pytorch_transformer_model(device)
    no_decay_params = [{'params' : ['decoder.bias',
                                    'encoder.bias',
                                    'transformer_encoder.layers.0.linear1.bias',
                                    'transformer_encoder.layers.0.linear2.bias',
                                    'transformer_encoder.layers.0.norm1.bias',
                                    'transformer_encoder.layers.0.norm2.bias',
                                    'transformer_encoder.layers.0.self_attn.in_proj_bias',
                                    'transformer_encoder.layers.0.self_attn.out_proj.bias',
                                    'transformer_encoder.layers.1.linear1.bias',
                                    'transformer_encoder.layers.1.linear2.bias',
                                    'transformer_encoder.layers.1.norm1.bias',
                                    'transformer_encoder.layers.1.norm2.bias',
                                    'transformer_encoder.layers.1.self_attn.in_proj_bias',
                                    'transformer_encoder.layers.1.self_attn.out_proj.bias'],
                       'alpha': 0.9, 'beta': 0.999, 'lambda_coef': 0.01, 'epsilon': 1e-6}]
    # decay_params = [{'params': ['decoder.weight',
    #                             'encoder.weight',
    #                             'transformer_encoder.layers.0.linear1.weight',
    #                             'transformer_encoder.layers.0.linear2.weight',
    #                             'transformer_encoder.layers.0.norm1.weight',
    #                             'transformer_encoder.layers.0.norm2.weight',
    #                             'transformer_encoder.layers.0.self_attn.in_proj_weight',
    #                             'transformer_encoder.layers.0.self_attn.out_proj.weight',
    #                             'transformer_encoder.layers.1.linear1.weight',
    #                             'transformer_encoder.layers.1.linear2.weight',
    #                             'transformer_encoder.layers.1.norm1.weight',
    #                             'transformer_encoder.layers.1.norm2.weight',
    #                             'transformer_encoder.layers.1.self_attn.in_proj_weight',
    #                             'transformer_encoder.layers.1.self_attn.out_proj.weight'],
    #                  'alpha': 0.9, 'beta': 0.999, 'lambda_coef': 0.0, 'epsilon': 1e-6}]
    # no_decay_params.extend(decay_params)
    optim_config = optim.LambConfig(no_decay_params, lr=0.001, alpha=0.9, beta=0.999, lambda_coef=0.0, epsilon=1e-6)
    trainer = orttrainer.ORTTrainer(model, model_desc, optim_config, loss_fn=my_loss, options=options)
    experimental_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        exp_loss, exp_preds = trainer.train_step(data, targets)
        experimental_loss.append(exp_loss.cpu())

    # Setup legacy API
    torch.manual_seed(seed)
    set_seed(seed)

    def map_optimizer_attributes(name):
        no_decay_keys = ["bias", "gamma", "beta", "LayerNorm"]
        no_decay = any(no_decay_key in name for no_decay_key in no_decay_keys)
        if no_decay:
            return {"alpha": 0.9, "beta": 0.999, "lambda": 0.01, "epsilon": 1e-6}
        else:
            return {"alpha": 0.9, "beta": 0.999, "lambda": 0.0, "epsilon": 1e-6}

    model, (model_desc, lr_desc), _, _, _, _, _ = _load_pytorch_transformer_model(device, legacy_api=True)
    legacy_trainer = Legacy_ORTTrainer(model, my_loss, model_desc, "LambOptimizer", map_optimizer_attributes,
                                       lr_desc, device=device,
                                       _use_deterministic_compute=True)
    # Training loop
    legacy_loss = []
    for i in range(total_steps):
        data, targets = batcher_fn(train_data, i)
        leg_loss, leg_p_reds = legacy_trainer.train_step(data, targets, torch.tensor([optim_config.lr]))
        legacy_loss.append(leg_loss.cpu())

    # # Compare legacy vs experimental APIs
    _test_helpers.assert_model_outputs(legacy_loss, experimental_loss, rtol=1e-6)
