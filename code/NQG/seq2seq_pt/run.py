from __future__ import division

import s2s
import argparse
import torch
import torch.nn as nn
from torch import cuda
from torch.autograd import Variable
import math
import time
import logging

from s2s.xinit import xavier_normal, xavier_uniform
import os
import xargs


def addPair(f1, f2):
    for x, y1 in zip(f1, f2):
        yield (x, y1)
    yield (None, None)

def load_dev_data(translator, src_file, bio_file, feat_files, tgt_file):
    dataset, raw = [], []
    srcF = open(src_file, encoding='utf-8')
    tgtF = open(tgt_file, encoding='utf-8')
    if opt.answer == 'embedding':
        bioF = open(bio_file, encoding='utf-8')
    if opt.feature:
        featFs = [open(x, encoding='utf-8') for x in feat_files]

    src_batch, tgt_batch = [], []
    bio_batch, feats_batch = [], []
    for line, tgt in addPair(srcF, tgtF):
        if (line is not None) and (tgt is not None):
            src_tokens = line.strip().split(' ')
            src_batch += [src_tokens]
            tgt_tokens = tgt.strip().split(' ')
            tgt_batch += [tgt_tokens]
            if opt.answer == 'embedding':
                bio_tokens = bioF.readline().strip().split(' ')
                bio_batch += [bio_tokens]
            if opt.feature:
                feats_tokens = [reader.readline().strip().split((' ')) for reader in featFs]
                feats_batch += [feats_tokens]

            if len(src_batch) < opt.batch_size:
                continue
        else:
            # at the end of file, check last batch
            if len(src_batch) == 0:
                break
        data = translator.buildData(src_batch, bio_batch, feats_batch, tgt_batch)
        dataset.append(data)
        raw.append((src_batch, tgt_batch))
        src_batch, tgt_batch = [], []
        bio_batch, feats_batch = [], []
    srcF.close()
    if opt.answer == 'embedding':
        bioF.close()
    if opt.feature:
        for f in featFs:
            f.close()
    tgtF.close()
    return (dataset, raw)


############################## parse the options ##############################
parser = argparse.ArgumentParser(description='train.py')
xargs.add_data_options(parser)
xargs.add_model_options(parser)
xargs.add_train_options(parser)

opt = parser.parse_args()

if opt.resume:
    checkpoint = torch.load(opt.load_path)


############################## prepare the logger ##############################
logging.basicConfig(format='%(asctime)s [%(levelname)s:%(name)s]: %(message)s', level=logging.INFO)
log_file_name = time.strftime("%Y%m%d-%H%M%S") + '.log.txt'
if opt.log_home:
    log_file_name = os.path.join(opt.log_home, log_file_name)
file_handler = logging.FileHandler(log_file_name, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)-5.5s:%(name)s] %(message)s'))
logging.root.addHandler(file_handler)
logger = logging.getLogger(__name__)

logger.info('My PID is {0}'.format(os.getpid()))
logger.info('PyTorch version: {0}'.format(str(torch.__version__)))
logger.info(opt)

if torch.cuda.is_available() and not opt.gpus:
    logger.info("WARNING: You have a CUDA device, so you should probably run with -gpus 0")

if opt.seed > 0:
    torch.manual_seed(opt.seed)

if opt.gpus:
    if opt.cuda_seed > 0:
        torch.cuda.manual_seed(opt.cuda_seed)
    cuda.set_device(opt.gpus[0])

logger.info('My seed is {0}'.format(torch.initial_seed()))
logger.info('My cuda seed is {0}'.format(torch.cuda.initial_seed()))


############################## prepare training dataset ##############################
import onlinePreprocess
onlinePreprocess.lower = opt.lower_input
onlinePreprocess.seq_length = opt.max_sent_length
onlinePreprocess.shuffle = 1 if opt.process_shuffle else 0

from onlinePreprocess import prepare_data_online
dataset = prepare_data_online(opt.copy, opt.answer, opt.feature, opt.train_src, opt.src_vocab, opt.train_bio, opt.bio_vocab, opt.train_feats,
                              opt.feat_vocab, opt.train_tgt, opt.tgt_vocab)
trainData = s2s.Dataset(dataset['train']['src'], dataset['train']['bio'], dataset['train']['feats'],
                        dataset['train']['tgt'], dataset['train']['switch'], dataset['train']['c_tgt'],
                        opt.batch_size, opt.gpus, opt.copy, opt.answer, opt.feature)
dicts = dataset['dicts']
logger.info(' * vocabulary size. source = %d; target = %d' %
            (dicts['src'].size(), dicts['tgt'].size()))
logger.info(' * number of training sentences. %d' %
            len(dataset['train']['src']))
logger.info(' * maximum batch size. %d' % opt.batch_size)

logger.info('Building model...')


############################## define and prepare model ##############################
encoder = s2s.Models.Encoder(opt, dicts['src'])
decoder = s2s.Models.Decoder(opt, dicts['tgt'])
decIniter = s2s.Models.DecInit(opt)
generator = nn.Sequential(
    nn.Linear(opt.dec_rnn_size // opt.maxout_pool_size, dicts['tgt'].size()),  # TODO: fix here
    # nn.LogSoftmax(dim=1)
    nn.Softmax(dim=1)
)

if opt.resume:
    dict_en, dict_de, dict_in = {}, {}, {}
    for k, v in checkpoint['model'].items():
        if k.startswith('encoder'):
            dict_en[k.lstrip('encoder').lstrip('.')] = v
        elif k.startswith('decoder'):
            dict_de[k.lstrip('decoder').lstrip('.')] = v
        else:
            dict_in[k.lstrip('decIniter').lstrip('.')] = v
    encoder.load_state_dict(dict_en)
    decoder.load_state_dict(dict_de)
    decIniter.load_state_dict(dict_in)

    generator.load_state_dict(checkpoint['generator'])

model = s2s.Models.NMTModel(encoder, decoder, decIniter)
model.generator = generator

translator = s2s.Translator(opt, model, dataset)

if len(opt.gpus) >= 1:
    model.cuda()
    generator.cuda()
else:
    model.cpu()
    generator.cpu()

# if len(opt.gpus) > 1:
#     model = nn.DataParallel(model, device_ids=opt.gpus, dim=1)
#     generator = nn.DataParallel(generator, device_ids=opt.gpus, dim=0)
if not opt.resume:
    for pr_name, p in model.named_parameters():
        logger.info(pr_name)
        # p.data.uniform_(-opt.param_init, opt.param_init)
        if p.dim() == 1:
            # p.data.zero_()
            p.data.normal_(0, math.sqrt(6 / (1 + p.size(0))))
        else:
            nn.init.xavier_normal_(p, math.sqrt(3))

    encoder.load_pretrained_vectors(opt)
    decoder.load_pretrained_vectors(opt)
else:
    model.flatten_parameters() # make RNN parameters contiguous


############################## prepare development dataset ##############################
validData = None
devData, train_dataset = None, None

if opt.dev_input_src and opt.dev_ref:
    validData = load_dev_data(translator, opt.dev_input_src, opt.dev_bio, opt.dev_feats, opt.dev_ref)

    if opt.result_path:
        dev_dataset = prepare_data_online(opt.copy, opt.answer, opt.feature, opt.dev_input_src, opt.src_vocab, opt.dev_bio, opt.bio_vocab, opt.dev_feats,
                                      opt.feat_vocab, opt.dev_ref, opt.tgt_vocab)
        devData = s2s.Dataset(dev_dataset['train']['src'], dev_dataset['train']['bio'], dev_dataset['train']['feats'],
                                dev_dataset['train']['tgt'], dev_dataset['train']['switch'], dev_dataset['train']['c_tgt'],
                                opt.batch_size, opt.gpus, opt.copy, opt.answer, opt.feature)

        train_dataset = load_dev_data(translator, opt.train_src, opt.train_bio, opt.train_feats, opt.train_tgt)


############################## define and prepare loss ##############################
vocabSize = dataset['dicts']['tgt'].size()
weight = torch.ones(vocabSize)
weight[s2s.Constants.PAD] = 0
loss = s2s.Loss.NLLLoss(weight, size_average=False, copy_loss=opt.copy)
if opt.gpus:
    loss.cuda()


############################## define and prepare optimizer ##############################
if opt.resume:
    optim = checkpoint['optim']
else:
    optim = s2s.Optim(
        opt.optim, opt.learning_rate,
        max_grad_norm=opt.max_grad_norm,
        max_weight_value=opt.max_weight_value,
        lr_decay=opt.learning_rate_decay,
        start_decay_at=opt.start_decay_at,
        decay_bad_count=opt.halve_lr_bad_count
    )
    optim.set_parameters(model.parameters())


######################################## train ########################################
evaluator = s2s.Evaluator(opt, translator)

trainer = s2s.SupervisedTrainer(model=model, loss=loss, evaluator=evaluator,
    optim=optim, logger=logger, opt=opt, trainData=trainData,
    validData=validData, dataset=dataset)

trainer.trainModel(train_dataset, devData)
