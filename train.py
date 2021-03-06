import time
import numpy as np
import os
import io
import shutil
import tensorflow as tf
import argparse
from keras.callbacks import LearningRateScheduler, TensorBoard, ModelCheckpoint, Callback
from keras.utils import plot_model
from keras.optimizers import Adam, SGD
import keras.backend as K


from model import EAST_model
from losses import dice_loss,rbox_loss
import data_processor

parser = argparse.ArgumentParser()
parser.add_argument('--input_size', type=int, default=512) # input size for training of the network
parser.add_argument('--batch_size', type=int, default=16) # batch size for training
parser.add_argument('--nb_workers', type=int, default=4) # number of processes to spin up when using process based threading, as defined in https://keras.io/models/model/#fit_generator
parser.add_argument('--init_learning_rate', type=float, default=0.0001) # initial learning rate
parser.add_argument('--lr_decay_rate', type=float, default=0.94) # decay rate for the learning rate
parser.add_argument('--lr_decay_steps', type=int, default=130) # number of steps after which the learning rate is decayed by decay rate
parser.add_argument('--max_epochs', type=int, default=800) # maximum number of epochs
parser.add_argument('--checkpoint_path', type=str, default='tmp/east_resnet_50_rbox') # path to a directory to save model checkpoints during training
parser.add_argument('--save_checkpoint_epochs', type=int, default=10) # period at which checkpoints are saved (defaults to every 10 epochs)
parser.add_argument('--last_epoch_train',type=int,default=0)
parser.add_argument('--restore_model', type=str, default='')
parser.add_argument('--training_data_path', type=str, default='../data/ICDAR2015/train_data') # path to training data
parser.add_argument('--validation_data_path', type=str, default='../data/MLT/val_data_latin') # path to validation data
parser.add_argument('--max_image_large_side', type=int, default=1280) # maximum size of the large side of a training image before cropping a patch for training
parser.add_argument('--max_text_size', type=int, default=800) # maximum size of a text instance in an image; image resized if this limit is exceeded
parser.add_argument('--min_text_size', type=int, default=10) # minimum size of a text instance; if smaller, then it is ignored during training
parser.add_argument('--min_crop_side_ratio', type=float, default=0.1) # the minimum ratio of min(H, W), the smaller side of the image, when taking a random crop from thee input image
parser.add_argument('--geometry', type=str, default='RBOX') # geometry type to be used; only RBOX is implemented now, but the original paper also uses QUAD
parser.add_argument('--suppress_warnings_and_error_messages', type=bool, default=True) # whether to show error messages and warnings during training (some error messages during training are expected to appear because of the way patches for training are created)
FLAGS = parser.parse_args()

lastEpoch = FLAGS.last_epoch_train

class CustomModelCheckpoint(Callback):
    def __init__(self, model, path, period, save_weights_only):
        super(CustomModelCheckpoint, self).__init__()
        self.period = period
        self.path = path
        # We set the model (non multi gpu) under an other name
        self.model_for_saving = model
        self.epochs_since_last_save = 0
        self.save_weights_only = save_weights_only

    def on_epoch_end(self, epoch, logs=None):
        self.epochs_since_last_save += 1
        if self.epochs_since_last_save >= self.period:
            self.epochs_since_last_save = 0
            if self.save_weights_only:
                self.model_for_saving.save_weights(self.path.format(epoch=epoch + lastEpoch + 1, **logs), overwrite=True)
            else:
                self.model_for_saving.save(self.path.format(epoch=epoch + lastEpoch + 1, **logs), overwrite=True)


class ValidationEvaluator(Callback):
    def __init__(self, validation_data, validation_log_dir, period=5):
        super(Callback, self).__init__()

        self.period = period
        self.validation_data = validation_data
        self.validation_log_dir = validation_log_dir
        self.val_writer = tf.summary.create_file_writer(self.validation_log_dir)

    def on_epoch_end(self, epoch, logs={}):
        if (epoch + 1) % self.period == 0:
            val_loss, val_score_map_loss, val_geo_map_loss = self.model.evaluate([self.validation_data[0], self.validation_data[1], self.validation_data[2], self.validation_data[3]],
                                                                                 [self.validation_data[3], self.validation_data[4]],
                                                                                 batch_size=FLAGS.batch_size)
            print('\nEpoch %d: val_loss: %.4f, val_score_map_loss: %.4f, val_geo_map_loss: %.4f' % (epoch + lastEpoch+1, val_loss, val_score_map_loss, val_geo_map_loss))
            with self.val_writer.as_default():
                tf.summary.scalar('loss',val_loss,step = epoch+lastEpoch+1)
                tf.summary.scalar('pred_score_map_loss',val_score_map_loss,step = epoch+lastEpoch+1)
                tf.summary.scalar('pred_geo_map_loss',val_geo_map_loss,step = epoch+lastEpoch+1)

            self.val_writer.flush()

def lr_decay(epoch):
    return FLAGS.init_learning_rate * np.power(FLAGS.lr_decay_rate, epoch + lastEpoch // FLAGS.lr_decay_steps)

def main(argv = None):
    if not os.path.exists(FLAGS.checkpoint_path):
        os.mkdir(FLAGS.checkpoint_path)
    else:
        #if not FLAGS.restore:
        #    shutil.rmtree(FLAGS.checkpoint_path)
        #    os.mkdir(FLAGS.checkpoint_path)
        shutil.rmtree(FLAGS.checkpoint_path)
        os.mkdir(FLAGS.checkpoint_path)

    train_data_generator = data_processor.generator(FLAGS)
    train_samples_count = data_processor.count_samples(FLAGS)

    val_data = data_processor.load_data(FLAGS)

    print('Training with 1 GPU')
    east = EAST_model(FLAGS.input_size)
    EASTmodel = east.model
    
    score_map_loss_weight = K.variable(0.01, name='score_map_loss_weight')
    small_text_weight = K.variable(0., name='small_text_weight')

    lr_scheduler = LearningRateScheduler(lr_decay)
    ckpt = CustomModelCheckpoint(model=east.model, path=FLAGS.checkpoint_path + '/model-{epoch:02d}.h5', period=FLAGS.save_checkpoint_epochs, save_weights_only=True)
    validation_evaluator = ValidationEvaluator(val_data, validation_log_dir=FLAGS.checkpoint_path + '/val')
    callbacks = [lr_scheduler, ckpt, validation_evaluator]

    opt = Adam(FLAGS.init_learning_rate)

    if FLAGS.last_epoch_train != 0:
        p = FLAGS.checkpoint_path + '/model-{epoch:02d}.h5'.format(epoch = FLAGS.last_epoch_train)
        EASTmodel.load_weight(p)

    EASTmodel.compile(loss=[dice_loss(east.overly_small_text_region_training_mask,east.text_region_boundary_training_mask,score_map_loss_weight,small_text_weight),
                                  rbox_loss(east.overly_small_text_region_training_mask,east.text_region_boundary_training_mask,small_text_weight,east.target_score_map)],
                  optimizer = opt,
                  loss_weights=[1.,1.])
    EASTmodel.summary()
    model_json = east.model.to_json()
    with open(FLAGS.checkpoint_path + '/model.json', 'w') as json_file:
        json_file.write(model_json)
    
    history = EASTmodel.fit_generator(train_data_generator,epochs=FLAGS.max_epochs,steps_per_epoch = train_samples_count/FLAGS.batch_size, workers=FLAGS.nb_workers, use_multiprocessing=True, max_queue_size=10, callbacks=callbacks, verbose=1)