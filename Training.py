from sacred import Experiment
import tensorflow as tf
import numpy as np
import os

from Input import urmp_input
import Utils
import Test
import Models.UnetAudioSeparator
import Models.ConditionalUnetAudioSeparator

from tensorflow.contrib.cluster_resolver import TPUClusterResolver
from tensorflow.contrib import summary
from tensorflow.contrib.tpu.python.tpu import tpu_config
from tensorflow.contrib.tpu.python.tpu import tpu_estimator
from tensorflow.contrib.tpu.python.tpu import tpu_optimizer
from tensorflow.contrib.tpu.python.tpu import bfloat16
from tensorflow.python.estimator import estimator

ex = Experiment('Conditioned-Waveunet')

@ex.config
def cfg():
    # Base configuration
    model_config = {"mode": 'train_and_eval', # 'predict'
                    "log_dir": "logs", # Base folder for logs files
                    "batch_size": 64, # Batch size
                    "init_sup_sep_lr": 1e-5, # Supervised separator learning rate
                    "epoch_it": 2000, # Number of supervised separator steps per epoch
                    "training_steps": 2000*100, # Number of training steps per training
                    "evaluation_steps": 1000,
                    "use_tpu": True,
                    "use_bfloat16": True,
                    "load_model": True,
                    "predict_only": False,
                    "write_audio_summaries": False,
                    "audio_summaries_every_n_steps": 10000,
                    "decay_steps": 2000,
                    "decay_rate": 0.96,
                    'num_layers': 12, # How many U-Net layers
                    'filter_size': 15, # For Wave-U-Net: Filter size of conv in downsampling block
                    'merge_filter_size': 5, # For Wave-U-Net: Filter size of conv in upsampling block
                    'num_initial_filters': 24, # Number of filters for convolution in first layer of network
                    "num_frames": 16384, # DESIRED number of time frames in the output waveform per samples (could be changed when using valid padding)
                    'expected_sr': 22050,  # Downsample all audio input to this sampling rate
                    'mono_downmix': True,  # Whether to downsample the audio input
                    'output_type': 'direct', # Type of output layer, either "direct" or "difference". Direct output: Each source is result of tanh activation and independent. DIfference: Last source output is equal to mixture input - sum(all other sources)
                    'context': False, # Type of padding for convolutions in separator. If False, feature maps double or half in dimensions after each convolution, and convolutions are padded with zeros ("same" padding). If True, convolution is only performed on the available mixture input, thus the output is smaller than the input
                    'network': 'unet', # Type of network architecture, either unet (our model) or unet_spectrogram (Jansson et al 2017 model)
                    'upsampling': 'linear', # Type of technique used for upsampling the feature maps in a unet architecture, either 'linear' interpolation or 'learned' filling in of extra samples
                    'task': 'voice', # Type of separation task. 'voice' : Separate music into voice and accompaniment. 'multi_instrument': Separate music into guitar, bass, vocals, drums and other (Sisec)
                    'augmentation': True, # Random attenuation of source signals to improve generalisation performance (data augmentation)
                    'raw_audio_loss': True, # Only active for unet_spectrogram network. True: L2 loss on audio. False: L1 loss on spectrogram magnitudes for training and validation and test loss
                    'experiment_id': np.random.randint(0,1000000)
                    }

    model_config["num_sources"] = 13 if model_config["task"] == "multi_instrument" else 2
    model_config["num_channels"] = 1 if model_config["mono_downmix"] else 2


@ex.named_config
def baseline():
    print("Training baseline model")


@ex.named_config
def baseline_stereo():
    print("Training baseline model with difference output and input context (valid convolutions)")
    model_config = {
        "output_type" : "difference",
        "context" : True,
        "mono_downmix" : False
    }


@ex.named_config
def full_multi_instrument():
    print("Training multi-instrument separation with best model")
    model_config = {
        "output_type": "difference",
        "context": True,
        "upsampling": "linear",
        "mono_downmix": True,
        "task": "multi_instrument"
    }

@ex.named_config
def urmp():
    print("Training multi-instrument separation with URMP dataset")
    model_config = {
        "dataset_name": "urmp",
        "data_path": "gs://modelcheckpoints/urmpv2",
        # "data_path": "/home/elias/projects/neural_network/tfrecords/train",
        "estimates_path": "estimates",
        "model_base_dir": "gs://modelcheckpoints", # Base folder for model checkpoints
        # "model_base_dir": "modelcheckpoints", # Base folder for model checkpoints
        "output_type": "difference",
        "context": True,
        "upsampling": "linear",
        "mono_downmix": True,
        "task": "multi_instrument"
    }

@ex.named_config
def musdb():
    print("Training multi-instrument separation with MusDB dataset")
    model_config = {
        "dataset_name": "musdb",
        "data_path": "gs://modelcheckpoints/",
        "estimates_path": "estimates",
        "model_base_dir": "gs://modelcheckpoints", # Base folder for model checkpoints
        "output_type": "difference",
        "context": True,
        "upsampling": "linear",
        "mono_downmix": True,
        "task": "multi_instrument"
    }

@ex.named_config
def baseline_comparison():
    model_config = {
        "batch_size": 4, # Less output since model is so big.

        "output_type": "difference",
        "context": True,
        "num_frames" : 768*127 + 1024,
        "duration" : 13,
        "expected_sr" : 8192,
        "num_initial_filters" : 34
    }

@ex.capture
def unet_separator(features, labels, mode, params):

    # Define host call function
    def host_call_fn(gs, loss, lr,
            mix=None,
            gt_sources=None,
            est_sources=None):
            """Training host call. Creates scalar summaries for training metrics.
            This function is executed on the CPU and should not directly reference
            any Tensors in the rest of the `model_fn`. To pass Tensors from the
            model to the `metric_fn`, provide as part of the `host_call`. See
            https://www.tensorflow.org/api_docs/python/tf/contrib/tpu/TPUEstimatorSpec
            for more information.
            Arguments should match the list of `Tensor` objects passed as the second
            element in the tuple passed to `host_call`.
            Args:
              gs: `Tensor with shape `[batch]` for the global_step
              loss: `Tensor` with shape `[batch]` for the training loss.
              lr: `Tensor` with shape `[batch]` for the learning_rate.
              input: `Tensor` with shape `[batch, mix_samples, 1]`
              gt_sources: `Tensor` with shape `[batch, sources_n, output_samples, 1]`
              est_sources: `Tensor` with shape `[batch, sources_n, output_samples, 1]`
            Returns:
              List of summary ops to run on the CPU host.
            """
            gs = gs[0]
            with summary.create_file_writer(model_config["model_base_dir"]+os.path.sep+str(model_config["experiment_id"])).as_default():
                with summary.always_record_summaries():
                    summary.scalar('loss', loss[0], step=gs)
                    summary.scalar('learning_rate', lr[0], step=gs)
                if gs % 10000 == 0:
                    with summary.record_summaries_every_n_global_steps(model_config["audio_summaries_every_n_steps"]):
                        summary.audio('mix', mix, model_config['expected_sr'], max_outputs=model_config["num_sources"])
                        for source_id in range(gt_sources.shape[1].value):
                            summary.audio('gt_sources_{source_id}'.format(source_id=source_id), gt_sources[:, source_id, :, :],
                                          model_config['expected_sr'], max_outputs=model_config["num_sources"])
                            summary.audio('est_sources_{source_id}'.format(source_id=source_id), est_sources[:, source_id, :, :],
                                          model_config['expected_sr'], max_outputs=model_config["num_sources"])
            return summary.all_summary_ops()

    mix = features['mix']
    conditioning = features['labels']
    sources = labels
    model_config = params
    disc_input_shape = [model_config["batch_size"], model_config["num_frames"], 0]

    with bfloat16.bfloat16_scope():
        separator_class = Models.ConditionalUnetAudioSeparator.UnetAudioSeparator(
            model_config["num_layers"], model_config["num_initial_filters"],
            output_type=model_config["output_type"],
            context=model_config["context"],
            mono=model_config["mono_downmix"],
            upsampling=model_config["upsampling"],
            num_sources=model_config["num_sources"],
            filter_size=model_config["filter_size"],
            merge_filter_size=model_config["merge_filter_size"])

    sep_input_shape, sep_output_shape = separator_class.get_padding(np.array(disc_input_shape))

    # Input context that the input audio has to be padded ON EACH SIDE
    # TODO move this to dataset function
    assert mix.shape[1].value == sep_input_shape[1]
    if mode != tf.estimator.ModeKeys.PREDICT:
        pad_tensor = tf.constant([[0, 0], [0, 0], [2, 3], [0, 0]])
        sources = tf.pad(sources, pad_tensor, "CONSTANT")

    separator_func = separator_class.get_output

    # Compute loss.
    separator_sources = tf.stack(separator_func(mix, conditioning,
                                                True, not model_config["raw_audio_loss"],
                                                reuse=False), axis=1)

    if mode == tf.estimator.ModeKeys.PREDICT:
        predictions = {
            'mix': mix,
            'sources': separator_sources,
            'filename': features['filename'],
            'sample_id': features['sample_id']
        }
        return tpu_estimator.TPUEstimatorSpec(mode, predictions=predictions)

    separator_loss = tf.cast(tf.reduce_sum(tf.squared_difference(sources, separator_sources)), tf.float32)

    if mode != tf.estimator.ModeKeys.PREDICT:
        global_step = tf.train.get_global_step()
        sep_lr = tf.train.exponential_decay(
                     model_config['init_sup_sep_lr'],
                     global_step,
                     model_config['decay_steps'],
                     model_config['decay_rate'],
                     staircase=False,
                     name=None
                 )

        gs_t = tf.reshape(global_step, [1])
        loss_t = tf.reshape(separator_loss, [1])
        lr_t = tf.reshape(sep_lr, [1])

        if model_config["write_audio_summaries"]:
            host_call = (host_call_fn, [gs_t, loss_t, lr_t, mix, sources, separator_sources])
        else:
            host_call = (host_call_fn, [gs_t, loss_t, lr_t, tf.zeros((1)), tf.zeros((1)), tf.zeros((1))])

    # Creating evaluation estimator
    if mode == tf.estimator.ModeKeys.EVAL:
        def metric_fn(labels, predictions):
            mean_mse_loss = tf.metrics.mean_squared_error(labels, predictions)
            return {'mse': mean_mse_loss}

        eval_params = {'labels': sources,
                       'predictions': separator_sources}

        return tpu_estimator.TPUEstimatorSpec(
            mode=mode,
            loss=separator_loss,
            host_call=host_call,
            eval_metrics=(metric_fn, eval_params))


    # Create training op.
    # TODO add learning rate schedule
    # TODO add early stopping
    if mode == tf.estimator.ModeKeys.TRAIN:
        separator_vars = Utils.getTrainableVariables("separator")
        print("Sep_Vars: " + str(Utils.getNumParams(separator_vars)))
        print("Num of variables: " + str(len(tf.global_variables())))

        separator_solver = tf.train.AdamOptimizer(learning_rate=sep_lr)
        if model_config["use_tpu"]:
            separator_solver = tpu_optimizer.CrossShardOptimizer(separator_solver)

        train_op = separator_solver.minimize(separator_loss,
                                             var_list=separator_vars,
                                             global_step=global_step)
        return tpu_estimator.TPUEstimatorSpec(mode=mode,
                                              loss=separator_loss,
                                              host_call=host_call,
                                              train_op=train_op)


@ex.automain
def experiment(model_config):
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.logging.info("SCRIPT START")

    tf.logging.info("TPU resolver started")

    os.environ['PROJECT_NAME']='nnproj'
    os.environ['PROJECT_ZONE']='boh'
    os.environ['TPU_NAME']='bah'
    tpu_cluster_resolver = TPUClusterResolver(
        tpu=os.environ['TPU_NAME'],
        project=os.environ['PROJECT_NAME'],
        zone=os.environ['PROJECT_ZONE'])

    if model_config["use_tpu"]:
        config = tpu_config.RunConfig(
            cluster=tpu_cluster_resolver,
            model_dir=model_config['model_base_dir'] + os.path.sep + str(model_config["experiment_id"]),
            save_checkpoints_steps=500,
            save_summary_steps=250,
            tpu_config=tpu_config.TPUConfig(
                iterations_per_loop=500,
                num_shards=8,
                per_host_input_for_training=tpu_config.InputPipelineConfig.PER_HOST_V1))  # pylint: disable=line-too-long
    else:
        config = tpu_config.RunConfig(
            cluster=tpu_cluster_resolver,
            model_dir=model_config['model_base_dir'] + os.path.sep + str(model_config["experiment_id"]),
            save_checkpoints_steps=500,
            save_summary_steps=250)  # pylint: disable=line-too-long

    tf.logging.info("Creating datasets")
    urmp_train, urmp_eval, urmp_test = [urmp_input.URMPInput(
        mode=mode,
        data_dir=model_config['data_path'],
        transpose_input=False,
        use_bfloat16=model_config['use_bfloat16']) for mode in ['train', 'eval', 'test']]

    tf.logging.info("Assigning TPUEstimator")
    # Optimize in a +supervised fashion until validation loss worsens
    separator = tpu_estimator.TPUEstimator(
        use_tpu=model_config["use_tpu"],
        model_fn=unet_separator,
        config=config,
        train_batch_size=model_config['batch_size'],
        eval_batch_size=model_config['batch_size'],
        predict_batch_size=model_config['batch_size'],
        params={i: model_config[i] for i in model_config if (i != 'batch_size' and i != 'context')} # TODO: context
    )

    if model_config['load_model']:
        tf.logging.info("Load the model")
        current_step = estimator._load_global_step_from_checkpoint_dir(
            model_config['model_base_dir'] + os.path.sep + str(model_config["experiment_id"]))

    if model_config['mode'] == 'train_and_eval':
        tf.logging.info("Train the model")
        # Should be an early stopping here, but it will come with tf 1.10
        separator.train(
            input_fn=urmp_train.input_fn,
            steps=model_config['training_steps'])
        # ...zzz...
        tf.logging.info("Supervised training finished!")
        tf.logging.info("Evaluate model")
        # Evaluate the model.
        eval_result = separator.evaluate(
            input_fn=urmp_eval.input_fn,
            steps=model_config['evaluation_steps'])
        tf.logging.info('Evaluation results: %s' % eval_result)

    elif model_config['mode'] == 'predict':
        tf.logging.info("Test results and save predicted sources:")
        predictions = separator.predict(
            input_fn=urmp_test.input_fn)

        for prediction in predictions:
            Test.save_prediction(prediction,
                                 estimates_path=model_config["estimates_path"],
                                 sample_rate=model_config["expected_sr"])
        Utils.concat_and_upload(model_config["estimates_path"],
                                model_config['model_base_dir'] + os.path.sep + str(model_config["experiment_id"]))
