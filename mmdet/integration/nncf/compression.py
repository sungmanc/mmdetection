# Copyright (C) 2021 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
import os
import pathlib
import tempfile

import mmcv
import torch

from mmdet.utils import get_root_logger, prepare_mmdet_model_for_execution
from .utils import (check_nncf_is_enabled, get_nncf_version, is_nncf_enabled,
                    load_checkpoint, no_nncf_trace)


def get_nncf_metadata():
    """
    The function returns NNCF metadata that should be stored into a checkpoint.
    The metadata is used to check in wrap_nncf_model if the checkpoint should be used
    to resume NNCF training or initialize NNCF fields of NNCF-wrapped model.
    """
    check_nncf_is_enabled()
    return dict(nncf_enable_compression=True, nncf_version=get_nncf_version())


def is_state_nncf(state):
    """
    The function uses metadata stored in a dict_state to check if the
    checkpoint was the result of trainning of NNCF-compressed model.
    See the function get_nncf_metadata above.
    """
    return bool(state.get('meta',{}).get('nncf_enable_compression', False))


def is_checkpoint_nncf(path):
    """
    The function uses metadata stored in a checkpoint to check if the
    checkpoint was the result of trainning of NNCF-compressed model.
    See the function get_nncf_metadata above.
    """
    try:
        checkpoint = torch.load(path, map_location='cpu')
        return is_state_nncf(checkpoint)
    except FileNotFoundError:
        return False


def get_nncf_config_from_meta(path):
    """
    The function uses metadata stored in a checkpoint to restore the nncf
    part of the model config.
    """
    logger = get_root_logger()
    checkpoint = torch.load(path, map_location='cpu')
    meta = checkpoint.get('meta', {})

    nncf_enable_compression = meta.get('nncf_enable_compression', False)
    assert nncf_enable_compression, \
        'get_nncf_config_from_meta should be run for NNCF-compressed checkpoints only'

    config_text = meta['config']

    with tempfile.NamedTemporaryFile(prefix='config_', suffix='.py',
                                     mode='w', delete=False) as f_tmp:
        f_tmp.write(config_text)
        tmp_name = f_tmp.name
    cfg = mmcv.Config.fromfile(tmp_name)
    os.unlink(tmp_name)

    nncf_config = cfg.get('nncf_config')

    assert isinstance(nncf_config, dict), (
        f'Wrong nncf_config part of the config saved in the metainfo'
        f' of the snapshot {path}:'
        f' nncf_config={nncf_config}')

    nncf_config_part = {
        'nncf_config': nncf_config,
        'find_unused_parameters': True
    }
    if nncf_config_part['nncf_config'].get('log_dir'):
        # TODO(LeonidBeynenson): improve work with log dir
        log_dir = tempfile.mkdtemp(prefix='nncf_output_')
        nncf_config_part['nncf_config']['log_dir'] = log_dir

    logger.info(f'Read nncf config from meta nncf_config_part={nncf_config_part}')
    return nncf_config_part


def extract_model_and_compression_states(resuming_checkpoint):
    """
    The function return from checkpoint state_dict and compression_state.
    """
    if resuming_checkpoint is None:
        return None, None
    model_state_dict = resuming_checkpoint.get("model" if "model" in resuming_checkpoint else "state_dict")
    compression_state = resuming_checkpoint.get("compression_state")
    return model_state_dict, compression_state


def wrap_nncf_model(model,
                    cfg,
                    distributed=False,
                    val_dataloader=None,
                    dataloader_for_init=None,
                    get_fake_input_func=None,
                    init_state_dict=None,
                    is_accuracy_aware=False,
                    is_alt_ssd_export=False):
    """
    The function wraps mmdet model by NNCF
    Note that the parameter `get_fake_input_func` should be the function `get_fake_input`
    -- cannot import this function here explicitly
    """

    check_nncf_is_enabled()

    from nncf import NNCFConfig
    from nncf.torch import create_compressed_model
    from nncf.torch import register_default_init_args
    from nncf.torch import load_state
    from nncf.torch.dynamic_graph.io_handling import nncf_model_input
    from nncf.torch.dynamic_graph.io_handling import wrap_nncf_model_outputs_with_objwalk
    from nncf.torch.dynamic_graph.trace_tensor import TracedTensor
    from nncf.torch.initialization import PTInitializingDataLoader

    class MMInitializeDataLoader(PTInitializingDataLoader):
        def get_inputs(self, dataloader_output):
            # redefined PTInitializingDataLoader because
            # of DataContainer format in mmdet
            kwargs = {k: v.data[0] for k, v in dataloader_output.items()}
            return (), kwargs

    pathlib.Path(cfg.work_dir).mkdir(parents=True, exist_ok=True)
    nncf_config = NNCFConfig(cfg.nncf_config)
    logger = get_root_logger(cfg.log_level)
    resuming_state_dict = None

    def model_eval_fn(model):
        """
        Runs evaluation of the model on the validation set and
        returns the target metric value.
        Used to evaluate the original model before compression
        if NNCF-based accuracy-aware training is used.
        """
        if val_dataloader is None:
            raise RuntimeError('Cannot perform model evaluation on the validation '
                               'dataset since the validation data loader was not passed '
                               'to wrap_nncf_model')
        from mmdet.apis import single_gpu_test, multi_gpu_test
        from functools import partial

        metric_name = nncf_config.get('target_metric_name')
        forward_backup = model.forward
        model_forward = type(model).forward
        model.forward = model_forward.__get__(model)
        model.forward = partial(model.forward, return_loss=False)
        prepared_model = prepare_mmdet_model_for_execution(model, cfg, distributed)

        logger.info(f'Calculating an original model accuracy')

        if distributed:
            dist_eval_res = [None]
            results = multi_gpu_test(prepared_model, val_dataloader, gpu_collect=True)
            if torch.distributed.get_rank() == 0:
                eval_res = val_dataloader.dataset.evaluate(results)
                if metric_name not in eval_res:
                    raise RuntimeError(f'Cannot find {metric_name} metric in '
                                       'the evaluation result dict')
                dist_eval_res[0] = eval_res

            torch.distributed.broadcast_object_list(dist_eval_res, src=0)
            model.forward = forward_backup
            return dist_eval_res[0][metric_name]
        else:
            results = single_gpu_test(prepared_model, val_dataloader, show=False)
            eval_res = val_dataloader.dataset.evaluate(results, metric=metric_name)

            if metric_name not in eval_res:
                raise RuntimeError(f'Cannot find {metric_name} metric in '
                                   f'the evaluation result dict {eval_res.keys()}')

            model.forward = forward_backup
            return eval_res[metric_name]

    if dataloader_for_init:
        wrapped_loader = MMInitializeDataLoader(dataloader_for_init)
        eval_fn = model_eval_fn if is_accuracy_aware else None
        nncf_config = register_default_init_args(nncf_config, wrapped_loader,
                                                 model_eval_fn=eval_fn,
                                                 device=next(model.parameters()).device)

    if cfg.get('resume_from'):
        checkpoint_path = cfg.get('resume_from')
        assert is_checkpoint_nncf(checkpoint_path), (
            'It is possible to resume training with NNCF compression from NNCF checkpoints only. '
            'Use "load_from" with non-compressed model for further compression by NNCF.')
    elif cfg.get('load_from'):
        checkpoint_path = cfg.get('load_from')
        if not is_checkpoint_nncf(checkpoint_path):
            checkpoint_path = None
            logger.info('Received non-NNCF checkpoint to start training '
                        '-- initialization of NNCF fields will be done')
    else:
        checkpoint_path = None

    if not dataloader_for_init and not checkpoint_path and not init_state_dict:
        logger.warning('Either dataloader_for_init or NNCF pre-trained '
                       'model checkpoint should be set. Without this, '
                       'quantizers will not be initialized')

    if checkpoint_path:
        logger.info(f'Loading NNCF checkpoint from {checkpoint_path}')
        logger.info(
            'Please, note that this first loading is made before addition of '
            'NNCF FakeQuantize nodes to the model, so there may be some '
            'warnings on unexpected keys')
        compression_state = load_checkpoint(model, checkpoint_path)
        logger.info(f'Loaded NNCF checkpoint from {checkpoint_path}')
    elif init_state_dict:
        resuming_state_dict = init_state_dict.get("model")
        compression_state = init_state_dict.get("compression_state")
    else:
        compression_state = None

    if "nncf_compress_postprocessing" in cfg:
        # NB: This parameter is used to choose if we should try to make NNCF compression
        #     for a whole model graph including postprocessing (`nncf_compress_postprocessing=True`),
        #     or make NNCF compression of the part of the model without postprocessing
        #     (`nncf_compress_postprocessing=False`).
        #     Our primary goal is to make NNCF compression of such big part of the model as
        #     possible, so `nncf_compress_postprocessing=True` is our primary choice, whereas
        #     `nncf_compress_postprocessing=False` is our fallback decision.
        #     When we manage to enable NNCF compression for sufficiently many models,
        #     we should keep one choice only.
        nncf_compress_postprocessing = cfg.get('nncf_compress_postprocessing')
        logger.debug('set should_compress_postprocessing='f'{nncf_compress_postprocessing}')
    else:
        nncf_compress_postprocessing = True

    def _get_fake_data_for_forward(cfg, nncf_config, get_fake_input_func):
        input_size = nncf_config.get("input_info").get('sample_size')
        assert get_fake_input_func is not None
        assert len(input_size) == 4 and input_size[0] == 1
        H, W, C = input_size[2], input_size[3], input_size[1]
        device = next(model.parameters()).device
        with no_nncf_trace():
            return get_fake_input_func(cfg, orig_img_shape=tuple([H, W, C]), device=device)

    def dummy_forward(model):
        fake_data = _get_fake_data_for_forward(cfg, nncf_config, get_fake_input_func)
        img, img_metas = fake_data["img"], fake_data["img_metas"]
        img[0] = nncf_model_input(img[0])
        if nncf_compress_postprocessing:
            if is_alt_ssd_export:
                img = img[0]
                img_metas = img_metas[0]
            ctx = model.forward_export_context(img_metas)
            logger.debug(f"NNCF will compress a postprocessing part of the model")
        else:
            ctx = model.forward_dummy_context(img_metas)
            logger.debug(f"NNCF will NOT compress a postprocessing part of the model")
        with ctx:
            model(img)

    def wrap_inputs(args, kwargs):
        # during dummy_forward
        if not len(kwargs):
            if is_alt_ssd_export:
                if not isinstance(args[0], TracedTensor):
                    nncf_input = nncf_model_input(args[0])
                return (nncf_input,), kwargs
            else:
                if not isinstance(args[0][0], TracedTensor):
                    args[0][0] = nncf_model_input(args[0][0])
                return args, kwargs

        # during building original graph
        if not kwargs.get('return_loss') and kwargs.get('forward_export'):
            return args, kwargs

        # during model's forward in export
        assert 'img' in kwargs, 'During model forward img must be in kwargs'
        img = kwargs['img']
        if isinstance(img, list):
            assert len(img) == 1, 'Input list must have a length 1'
            assert torch.is_tensor(img[0]), 'Input for a model must be a tensor'
            img[0] = nncf_model_input(img[0])
        else:
            assert torch.is_tensor(img), 'Input for a model must be a tensor'
            img = nncf_model_input(img)
        kwargs['img'] = img
        return args, kwargs

    model.dummy_forward_fn = dummy_forward
    export_method = type(model).export

    if 'log_dir' in nncf_config:
        os.makedirs(nncf_config['log_dir'], exist_ok=True)

    compression_ctrl, model = create_compressed_model(model,
                                                      nncf_config,
                                                      dummy_forward_fn=dummy_forward,
                                                      wrap_inputs_fn=wrap_inputs,
                                                      compression_state=compression_state)
    if resuming_state_dict:
        load_state(model, resuming_state_dict, is_resume=True)
    model.export = export_method.__get__(model)

    return compression_ctrl, model


def get_uncompressed_model(module):
    if not is_nncf_enabled():
        return module
    from nncf.torch.nncf_network import NNCFNetwork
    if isinstance(module, NNCFNetwork):
        return module.get_nncf_wrapped_model()
    return module


class AccuracyAwareLrUpdater:
    def __init__(self, lr_hook, runner, optimizer=None):
        self._lr_hook = lr_hook
        self._runner = runner
        if optimizer:
            runner.optimizer = optimizer
        self._lr_hook.before_run(runner)
        self._lr_hook.warmup_iters = 0

    def step(self, *args, **kwargs):
        pass

    @property
    def base_lrs(self):
        return self._lr_hook.base_lr

    @base_lrs.setter
    def base_lrs(self, value):
        self._lr_hook.base_lr = value
