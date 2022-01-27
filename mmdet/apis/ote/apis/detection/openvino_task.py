# Copyright (C) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.

import attr
import copy
import inspect
import json
import numpy as np
import os
import ote_sdk.usecases.exportable_code.demo as demo
import subprocess  # nosec
import sys
import tempfile
from addict import Dict as ADDict
from compression.api import DataLoader
from compression.engines.ie_engine import IEEngine
from compression.graph import load_model, save_model
from compression.graph.model_utils import compress_model_weights, get_nodes_by_type
from compression.pipeline.initializer import create_pipeline
from openvino.model_zoo.model_api.adapters import OpenvinoAdapter, create_core
from openvino.model_zoo.model_api.models import Model
from ote_sdk.entities.annotation import AnnotationSceneEntity
from ote_sdk.entities.datasets import DatasetEntity
from ote_sdk.entities.inference_parameters import InferenceParameters, default_progress_callback
from ote_sdk.entities.label_schema import LabelSchemaEntity
from ote_sdk.entities.model import (
    ModelEntity,
    ModelFormat,
    ModelOptimizationType,
    ModelPrecision,
    OptimizationMethod,
)
from ote_sdk.entities.model_template import TaskType
from ote_sdk.entities.optimization_parameters import OptimizationParameters
from ote_sdk.entities.resultset import ResultSetEntity
from ote_sdk.entities.task_environment import TaskEnvironment
from ote_sdk.serialization.label_mapper import LabelSchemaMapper, label_schema_to_bytes
from ote_sdk.usecases.evaluation.metrics_helper import MetricsHelper
from ote_sdk.usecases.exportable_code.inference import BaseInferencer
from ote_sdk.usecases.exportable_code.prediction_to_annotation_converter import DetectionBoxToAnnotationConverter, MaskToAnnotationConverter
from ote_sdk.usecases.exportable_code.utils import set_proper_git_commit_hash
from ote_sdk.usecases.tasks.interfaces.deployment_interface import IDeploymentTask
from ote_sdk.usecases.tasks.interfaces.evaluate_interface import IEvaluationTask
from ote_sdk.usecases.tasks.interfaces.inference_interface import IInferenceTask
from ote_sdk.usecases.tasks.interfaces.optimization_interface import IOptimizationTask, OptimizationType
from shutil import copyfile, copytree
from typing import Any, Dict, List, Optional, Tuple, Union
from zipfile import ZipFile

from mmdet.utils.logger import get_root_logger
from . import model_wrappers
from .configuration import OTEDetectionConfig

logger = get_root_logger()


class OpenVINODetectionInferencer(BaseInferencer):
    def __init__(
        self,
        hparams: OTEDetectionConfig,
        label_schema: LabelSchemaEntity,
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        """
        Inferencer implementation for OTEDetection using OpenVINO backend.

        :param hparams: Hyper parameters that the model should use.
        :param label_schema: LabelSchemaEntity that was used during model training.
        :param model_file: Path OpenVINO IR model definition file.
        :param weight_file: Path OpenVINO IR model weights file.
        :param device: Device to run inference on, such as CPU, GPU or MYRIAD. Defaults to "CPU".
        :param num_requests: Maximum number of requests that the inferencer can make. Defaults to 1.

        """

        model_adapter = OpenvinoAdapter(create_core(), model_file, weight_file, device=device, max_num_requests=num_requests)
        self.configuration = {**attr.asdict(hparams.postprocessing,
                              filter=lambda attr, value: attr.name not in ['header', 'description', 'type', 'visible_in_ui'])}
        self.model = Model.create_model('ssd', model_adapter, self.configuration, preload=True)
        self.converter = DetectionBoxToAnnotationConverter(label_schema)

    def pre_process(self, image: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        return self.model.preprocess(image)

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        detections = self.model.postprocess(prediction, metadata)

        return self.converter.convert_to_annotation(detections, metadata)

    def forward(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        return self.model.infer_sync(inputs)


class OpenVINOMaskInferencer(BaseInferencer):
    def __init__(
        self,
        hparams: OTEDetectionConfig,
        label_schema: LabelSchemaEntity,
        model_file: Union[str, bytes],
        weight_file: Union[str, bytes, None] = None,
        device: str = "CPU",
        num_requests: int = 1,
    ):
        model_adapter = OpenvinoAdapter(
          create_core(),
          model_file,
          weight_file,
          device=device,
          max_num_requests=num_requests)

        self.configuration = {
          **attr.asdict(
            hparams.postprocessing,
            filter=lambda attr, value: attr.name not in [
              'header', 'description', 'type', 'visible_in_ui'])}

        self.model = Model.create_model(
          'maskrcnn',
          model_adapter,
          self.configuration,
          preload=True)

        self.converter = MaskToAnnotationConverter(label_schema)

    def pre_process(self, image: np.ndarray) -> Tuple[Dict[str, np.ndarray], Dict[str, Any]]:
        return self.model.preprocess(image)

    def post_process(self, prediction: Dict[str, np.ndarray], metadata: Dict[str, Any]) -> AnnotationSceneEntity:
        detections = self.model.postprocess(prediction, metadata)

        return self.converter.convert_to_annotation(detections, metadata)

    def forward(self, inputs: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        return self.model.infer_sync(inputs)


class OTEOpenVinoDataLoader(DataLoader):
    def __init__(self, dataset: DatasetEntity, inferencer: BaseInferencer):
        self.dataset = dataset
        self.inferencer = inferencer

    def __getitem__(self, index):
        image = self.dataset[index].numpy
        annotation = self.dataset[index].annotation_scene
        inputs, metadata = self.inferencer.pre_process(image)

        return (index, annotation), inputs, metadata

    def __len__(self):
        return len(self.dataset)


class OpenVINODetectionTask(IDeploymentTask, IInferenceTask, IEvaluationTask, IOptimizationTask):
    def __init__(self, task_environment: TaskEnvironment):
        logger.info('Loading OpenVINO OTEDetectionTask')
        self.task_environment = task_environment
        self.model = self.task_environment.model
        self.task_type = self.task_environment.model_template.task_type
        self.confidence_threshold: float = 0.0
        self.model_name = task_environment.model_template.model_template_id
        self.inferencer = self.load_inferencer()
        logger.info('OpenVINO task initialization completed')

    @property
    def hparams(self):
        return self.task_environment.get_hyper_parameters(OTEDetectionConfig)

    def load_inferencer(self) -> Union[OpenVINODetectionInferencer, OpenVINOMaskInferencer] :
        _hparams = copy.deepcopy(self.hparams)
        self.confidence_threshold = float(np.frombuffer(self.model.get_data("confidence_threshold"), dtype=np.float32)[0])
        _hparams.postprocessing.confidence_threshold = self.confidence_threshold
        args = [
            _hparams,
            self.task_environment.label_schema,
            self.model.get_data("openvino.xml"),
            self.model.get_data("openvino.bin"),
        ]
        if self.task_type == TaskType.DETECTION:
            return OpenVINODetectionInferencer(*args)
        if self.task_type == TaskType.COUNTING:
            return OpenVINOMaskInferencer(*args)
        raise RuntimeError(f"Unknown OpenVINO Inferencer TaskType: {self.task_type}")

    def infer(self, dataset: DatasetEntity, inference_parameters: Optional[InferenceParameters] = None) -> DatasetEntity:
        logger.info('Start OpenVINO inference')
        update_progress_callback = default_progress_callback
        if inference_parameters is not None:
            update_progress_callback = inference_parameters.update_progress
        dataset_size = len(dataset)
        for i, dataset_item in enumerate(dataset, 1):
            predicted_scene = self.inferencer.predict(dataset_item.numpy)
            dataset_item.append_annotations(predicted_scene.annotations)
            update_progress_callback(int(i / dataset_size * 100))
        logger.info('OpenVINO inference completed')
        return dataset

    def evaluate(self,
                 output_result_set: ResultSetEntity,
                 evaluation_metric: Optional[str] = None):
        logger.info('Start OpenVINO metric evaluation')
        if evaluation_metric is not None:
            logger.warning(f'Requested to use {evaluation_metric} metric, but parameter is ignored. Use F-measure instead.')
        output_result_set.performance = MetricsHelper.compute_f_measure(output_result_set).get_performance()
        logger.info('OpenVINO metric evaluation completed')

    def deploy(self,
               output_model: ModelEntity) -> None:
        logger.info('Deploying the model')

        work_dir = os.path.dirname(demo.__file__)
        model_file = inspect.getfile(type(self.inferencer.model))
        parameters = {}
        parameters['type_of_model'] = self.inferencer.model.__model__
        parameters['converter_type'] = str(self.task_type)
        parameters['model_parameters'] = self.inferencer.configuration
        parameters['model_parameters']['labels'] = LabelSchemaMapper.forward(self.task_environment.label_schema)
        name_of_package = "demo_package"
        with tempfile.TemporaryDirectory() as tempdir:
            copyfile(os.path.join(work_dir, "setup.py"), os.path.join(tempdir, "setup.py"))
            copyfile(os.path.join(work_dir, "requirements.txt"), os.path.join(tempdir, "requirements.txt"))
            set_proper_git_commit_hash(os.path.join(tempdir, "requirements.txt"))
            copytree(os.path.join(work_dir, name_of_package), os.path.join(tempdir, name_of_package))
            config_path = os.path.join(tempdir, name_of_package, "config.json")
            with open(config_path, "w", encoding='utf-8') as f:
                json.dump(parameters, f, ensure_ascii=False, indent=4)
            # generate model.py
            if (inspect.getmodule(self.inferencer.model) in
               [module[1] for module in inspect.getmembers(model_wrappers, inspect.ismodule)]):
                copyfile(model_file, os.path.join(tempdir, name_of_package, "model.py"))
            # create wheel package
            subprocess.run([sys.executable, os.path.join(tempdir, "setup.py"), 'bdist_wheel',
                            '--dist-dir', tempdir, 'clean', '--all'])
            wheel_file_name = [f for f in os.listdir(tempdir) if f.endswith('.whl')][0]

            with ZipFile(os.path.join(tempdir, "openvino.zip"), 'w') as zip:
                zip.writestr(os.path.join("model", "model.xml"), self.model.get_data("openvino.xml"))
                zip.writestr(os.path.join("model", "model.bin"), self.model.get_data("openvino.bin"))
                zip.write(os.path.join(tempdir, "requirements.txt"), os.path.join("python", "requirements.txt"))
                zip.write(os.path.join(work_dir, "README.md"), os.path.join("python", "README.md"))
                zip.write(os.path.join(work_dir, "LICENSE"), os.path.join("python", "LICENSE"))
                zip.write(os.path.join(work_dir, "demo.py"), os.path.join("python", "demo.py"))
                zip.write(os.path.join(tempdir, wheel_file_name), os.path.join("python", wheel_file_name))
            with open(os.path.join(tempdir, "openvino.zip"), "rb") as file:
                output_model.exportable_code = file.read()
        logger.info('Deploying completed')

    def optimize(self,
                 optimization_type: OptimizationType,
                 dataset: DatasetEntity,
                 output_model: ModelEntity,
                 optimization_parameters: Optional[OptimizationParameters]):
        logger.info('Start POT optimization')

        if optimization_type is not OptimizationType.POT:
            raise ValueError('POT is the only supported optimization type for OpenVino models')

        data_loader = OTEOpenVinoDataLoader(dataset, self.inferencer)

        with tempfile.TemporaryDirectory() as tempdir:
            xml_path = os.path.join(tempdir, "model.xml")
            bin_path = os.path.join(tempdir, "model.bin")
            with open(xml_path, "wb") as f:
                f.write(self.model.get_data("openvino.xml"))
            with open(bin_path, "wb") as f:
                f.write(self.model.get_data("openvino.bin"))

            model_config = ADDict({
                'model_name': 'openvino_model',
                'model': xml_path,
                'weights': bin_path
            })

            model = load_model(model_config)

            if get_nodes_by_type(model, ['FakeQuantize']):
                raise RuntimeError("Model is already optimized by POT")

        engine_config = ADDict({
            'device': 'CPU'
        })

        stat_subset_size = self.hparams.pot_parameters.stat_subset_size
        preset = self.hparams.pot_parameters.preset.name.lower()

        algorithms = [
            {
                'name': 'DefaultQuantization',
                'params': {
                    'target_device': 'ANY',
                    'preset': preset,
                    'stat_subset_size': min(stat_subset_size, len(data_loader)),
                    'shuffle_data': True
                }
            }
        ]

        engine = IEEngine(config=engine_config, data_loader=data_loader, metric=None)

        pipeline = create_pipeline(algorithms, engine)

        compressed_model = pipeline.run(model)

        compress_model_weights(compressed_model)

        with tempfile.TemporaryDirectory() as tempdir:
            save_model(compressed_model, tempdir, model_name="model")
            with open(os.path.join(tempdir, "model.xml"), "rb") as f:
                output_model.set_data("openvino.xml", f.read())
            with open(os.path.join(tempdir, "model.bin"), "rb") as f:
                output_model.set_data("openvino.bin", f.read())
            output_model.set_data("confidence_threshold", np.array([self.confidence_threshold], dtype=np.float32).tobytes())

        output_model.set_data("label_schema.json", label_schema_to_bytes(self.task_environment.label_schema))

        # set model attributes for quantized model
        output_model.model_format = ModelFormat.OPENVINO
        output_model.optimization_type = ModelOptimizationType.POT
        output_model.optimization_methods = [OptimizationMethod.QUANTIZATION]
        output_model.precision = [ModelPrecision.INT8]

        self.model = output_model
        self.inferencer = self.load_inferencer()
        logger.info('POT optimization completed')
