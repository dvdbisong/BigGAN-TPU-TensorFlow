

from comet_ml import Experiment

from BigGAN_128 import BigGAN_128

import argparse
import subprocess
import os.path

import logging
import coloredlogs
logger = logging.getLogger(__name__)
coloredlogs.install(level='INFO', logger=logger)
coloredlogs.install(level='DEBUG', logger=logging.getLogger('ops'))
coloredlogs.install(level='DEBUG', logger=logging.getLogger('utils'))
coloredlogs.install(level='DEBUG', logger=logging.getLogger('BigGAN_128'))




from utils import *

"""parsing and configuration"""
def parse_args():
	desc = "Tensorflow implementation of BigGAN"
	parser = argparse.ArgumentParser(description=desc)
	parser.add_argument('--tag'              , action="append" , default=[])
	parser.add_argument('--phase'            , type=str        , default='train'                                           , help='train or test ?')
	
	parser.add_argument('--train-input-path' , type=str        , default='./datasets/atk-vclose/atk-vclose-r07.tfrecords')
	parser.add_argument('--model-dir'        , type=str        , default='model')
	parser.add_argument('--result-dir'       , type=str        , default='results')

	# SAGAN
	# batch_size = 256
	# base channel = 64
	# epoch = 100 (1M iterations)
	# self-attn-res = [64]

	parser.add_argument('--img-size'        , type=int             , default=128                               , help='The width and height of the input/output image')
	parser.add_argument('--img-ch'          , type=int             , default=3                                 , help='The number of channels in the input/output image')

	parser.add_argument('--epochs'          , type=int             , default=100                               , help='The number of training iterations')
	parser.add_argument('--train-steps'     , type=int             , default=10000                             , help='The number of training iterations')
	parser.add_argument('--eval-steps'      , type=int             , default=100                               , help='The number of eval iterations')
	parser.add_argument('--batch-size'      , type=int             , default=2048  , dest="_batch_size"        , help='The size of batch across all GPUs')
	parser.add_argument('--ch'              , type=int             , default=96                                , help='base channel number per layer')
	parser.add_argument('--layers'          , type=int             , default=5 )

	parser.add_argument('--use-tpu'         , action='store_true')
	parser.add_argument('--tpu-name'        , action='append'      , default=[] )
	parser.add_argument('--tpu-zone'		, type=str, default='us-central1-f')
	parser.add_argument('--num-shards'      , type=int             , default=8) # A single TPU has 8 shards
	parser.add_argument('--steps-per-loop'  , type=int             , default=10000)

	parser.add_argument('--disable-comet'   , action='store_false', dest='use_comet')

	parser.add_argument('--self-attn-res'   , action='append', default=[64] )

	parser.add_argument('--g-lr'            , type=float           , default=0.00005                           , help='learning rate for generator')
	parser.add_argument('--d-lr'            , type=float           , default=0.0002                            , help='learning rate for discriminator')

	# if lower batch size
	# g_lr = 0.0001
	# d_lr = 0.0004

	# if larger batch size
	# g_lr = 0.00005
	# d_lr = 0.0002

	parser.add_argument('--beta1'          , type=float    , default=0.0           , help='beta1 for Adam optimizer')
	parser.add_argument('--beta2'          , type=float    , default=0.9           , help='beta2 for Adam optimizer')
	parser.add_argument('--moving-decay'   , type=float    , default=0.9999        , help='moving average decay for generator')

	parser.add_argument('--z-dim'          , type=int      , default=128           , help='Dimension of noise vector')
	parser.add_argument('--sn'             , type=str2bool , default=True          , help='using spectral norm')

	parser.add_argument('--gan-type'       , type=str      , default='hinge'       , help='[gan / lsgan / wgan-gp / wgan-lp / dragan / hinge]')
	parser.add_argument('--ld'             , type=float    , default=10.0          , help='The gradient penalty lambda')
	parser.add_argument('--n-critic'       , type=int      , default=2             , help='The number of critic')

	parser.add_argument('--inception-score-num'     , type=int      , default=50000            , help='The number of sample images to use in inception score')
	parser.add_argument('--sample-num'     , type=int      , default=36            , help='The number of sample images to save')
	parser.add_argument('--test-num'       , type=int      , default=10            , help='The number of images generated by the test')

	parser.add_argument('--verbosity', type=str, default='WARNING')

	args = parser.parse_args()
	return check_args(args)



def check_args(args):
	tf.gfile.MakeDirs(suffixed_folder(args, args.result_dir))
	tf.gfile.MakeDirs("./temp/")

	assert args.epochs >= 1, "number of epochs must be larger than or equal to one"
	assert args._batch_size >= 1, "batch size must be larger than or equal to one"
	assert args.ch >= 8, "--ch cannot be less than 8 otherwise some dimensions of the network will be size 0"

	return args



def model_dir(args):
	return os.path.join(args.model_dir, *args.tag, model_name(args))



def parse_tfrecord_tf(params, record):
	'''
	Parse the records saved using the NVIDIA ProGAN dataset_tool.py

	Data is stored as CHW uint8 with values ranging 0-255
	Size is stored beside image byte strings
	Data is stored in files with suffix -rN.tfrecords

	N = 0 is the largest size, 128x128 in my personal ATK image build

	'''

	features = tf.parse_single_example(record, features={
		'shape': tf.FixedLenFeature([3], tf.int64),
		'data': tf.FixedLenFeature([], tf.string)})
	data = tf.decode_raw(features['data'], tf.uint8)

	# img = tf.reshape(data, features['shape']) # The way from ProGAN
	img = tf.reshape(data, [params['img_ch'], params['img_size'], params['img_size']])

	img = tf.transpose(img, [1,2,0]) # CHW => HWC
	img = tf.cast(img, tf.float32) / 127.5 - 1

	return img



def generic_input_fn(params, path, repeat=False):
	dataset = tf.data.TFRecordDataset([path])
	dataset = dataset.map(lambda record: parse_tfrecord_tf(params, record))
	dataset = dataset.shuffle(1000)

	if repeat:
		dataset = dataset.repeat()

	dataset = dataset.batch(params['batch_size'], drop_remainder=True)

	return dataset

def train_input_fn(params):
	return generic_input_fn(params, params['train_input_path'], repeat=True)

def eval_input_fn(params):
	return generic_input_fn(params, params['train_input_path'], repeat=True)

def predict_input_fn(params):
	count = max(params['sample_num'], params['batch_size'], params['inception_score_num'])

	data = np.zeros([count], dtype=np.float32)
	dataset = tf.data.Dataset.from_tensor_slices(data)
	dataset = dataset.batch(params['batch_size'], drop_remainder=True)
	return dataset


def main():
	args = parse_args()
	if args is None:
	  exit()

	tf.logging.set_verbosity(args.verbosity)
	log_args(args)

	gan = BigGAN_128(args)

	if args.use_tpu:
		cluster_resolver = tf.contrib.cluster_resolver.TPUClusterResolver(
			tpu=args.tpu_name,
			zone=args.tpu_zone)
		master = cluster_resolver.get_master()
	else:
		master = ''

	tpu_run_config = tf.contrib.tpu.RunConfig(
		master=master,
		evaluation_master=master,
		model_dir=model_dir(args),
		session_config=tf.ConfigProto(
			allow_soft_placement=True, 
			log_device_placement=False),
		tpu_config=tf.contrib.tpu.TPUConfig(args.steps_per_loop,
											args.num_shards),
	)

	tpu_estimator = tf.contrib.tpu.TPUEstimator(
		model_fn=lambda features, labels, mode, params: gan.tpu_model_fn(features, labels, mode, params),
		config = tpu_run_config,
		use_tpu=args.use_tpu,
		train_batch_size=args._batch_size,
		eval_batch_size=args._batch_size,
		predict_batch_size=args._batch_size,
		params=vars(args),
	)

	total_steps = 0

	if args.use_comet:
		experiment = Experiment(api_key="bRptcjkrwOuba29GcyiNaGDbj", project_name="BigGAN", workspace="davidhughhenrymack")
		experiment.log_parameters(vars(args))
		experiment.add_tags(args.tag)
		experiment.set_name(model_name(args))
	else:
		experiment = None

	if args.phase == 'train':
		for epoch in range(args.epochs):
			logger.info(f"Training epoch {epoch}")
			tpu_estimator.train(input_fn=train_input_fn, steps=args.train_steps)
			total_steps += args.train_steps
			experiment.set_step(total_steps)
			
			logger.info(f"Evaluate {epoch}")
			evaluation = tpu_estimator.evaluate(input_fn=eval_input_fn, steps=args.eval_steps)
			experiment.log_metrics(evaluation)
			logger.info(evaluation)
			save_evaluation(args, suffixed_folder(args, args.result_dir), evaluation, epoch, total_steps)

			logger.info(f"Generate predictions {epoch}")
			predictions = tpu_estimator.predict(input_fn=predict_input_fn)
			
			logger.info(f"Save predictions")
			save_predictions(args, suffixed_folder(args, args.result_dir), predictions, epoch, total_steps, experiment)




if __name__ == '__main__':
	main()

