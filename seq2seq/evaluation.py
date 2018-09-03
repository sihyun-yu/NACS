# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import logging
import torch
import subprocess
import re
import math
import numpy as np
from collections import defaultdict
import os
from torch import nn
from torch.autograd import Variable
from torchtext import data
from seq2seq.utils import UNK_TOKEN, PAD_TOKEN, EOS_TOKEN
from seq2seq.lib import bleu


logger = logging.getLogger(__name__)
use_cuda = torch.cuda.is_available()

LongTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor


def evaluate_all(model=None, batch_iter=None, src_vocab=None, trg_vocab=None,
                 src_vocab_tags=None, trg_vocab_tags=None, max_length=50,
                 scan_normalize=False):

    _, ppx, acc, _, _, _, _, _ = evaluate(
        model=model, batch_iter=batch_iter, src_vocab=src_vocab,
        trg_vocab=trg_vocab, src_vocab_tags=src_vocab_tags, trg_vocab_tags=trg_vocab_tags)

    # exact match (predicted)
    em, _, _ = evaluate_exact_match(
        model=model, batch_iter=batch_iter, trg_vocab=trg_vocab, max_length=max_length, scan_normalize=scan_normalize)

    bleu_score = evaluate_bleu(
        model=model, batch_iter=batch_iter, trg_vocab=trg_vocab, max_length=max_length)

    return acc, ppx, em, bleu_score


def evaluate(model=None, batch_iter=None, src_vocab=None, trg_vocab=None, src_vocab_tags=None, trg_vocab_tags=None):
    """
    Evaluate perplexity, accuracy (with teacher forcing), exact match (with teacher forcing)

    Warning: this function shows how well the model does next word prediction given the correct history of words.
    To know how well it predicts a sequence on its own, without being provided the correct history,
    use the other evaluation functions.

    Args:
        model:
        batch_iter:
        src_vocab:
        trg_vocab:

    Returns:
        total_loss: total loss incurred on the data
        ppx: perplexity
        acc: accuracy (%)
        em: exact match (%)
        n_words: number of target tokens in the iterator
        n_correct: number of tokens correct (given complete gold history)
        n_seqs: number of sequences in iterator
        n_seqs_correct: number of sequences 100% correct
    """

    total_loss = 0.
    n_words = 0
    n_correct = 0
    n_seqs = 0
    n_seqs_correct = 0
    n_src_tags = 0
    n_src_tags_correct = 0
    n_trg_tags = 0
    n_trg_tags_correct = 0

    pad_idx_src = src_vocab.stoi[PAD_TOKEN]
    pad_idx_tags_src = src_vocab_tags.stoi[PAD_TOKEN] if src_vocab_tags is not None else None
    pad_idx_trg = trg_vocab.stoi[PAD_TOKEN]
    pad_idx_tags_trg = trg_vocab_tags.stoi[PAD_TOKEN] if trg_vocab_tags is not None else None

    # criterion = nn.NLLLoss(reduce=True, size_average=False, ignore_index=pad_idx_trg)

    model.eval()

    for batch in iter(batch_iter):

        src_var, src_lengths = batch.src
        trg_var, trg_lengths = batch.trg
        src_tags_var = batch.src_tags if hasattr(batch, 'src_tags') else None
        trg_tags_var = batch.trg_tags if hasattr(batch, 'trg_tags') else None

        src_lengths = src_lengths.view(-1).tolist()
        trg_lengths = trg_lengths.view(-1).tolist()

        batch_size = trg_var.size(0)
        time_steps = trg_var.size(1)
        n_words += sum(trg_lengths)
        n_seqs += len(trg_lengths)

        predict_src_tags = True if src_tags_var is not None and model.src_tagger is not None else False
        predict_trg_tags = True if trg_tags_var is not None and model.trg_tagger is not None else False

        # it does not matter for exact match
        result = model(src_var=src_var, src_lengths=src_lengths, trg_var=trg_var, trg_lengths=trg_lengths,
                       max_length=time_steps, tf_ratio=1.,
                       src_tags_var=src_tags_var, trg_tags_var=trg_tags_var,
                       predict_src_tags=predict_src_tags, predict_trg_tags=predict_trg_tags,
                       return_attention=False, return_states=False)

        predictions = result['preds']
        src_tag_predictions = result['src_tag_preds'] if predict_src_tags else None
        trg_tag_predictions = result['trg_tag_preds'] if predict_trg_tags else None
        loss_dict = result['loss']
        result = None

        # log_probs = result['log_probs']
        # voc_size = log_probs.size(2)

        # loss
        # log_probs_2d = log_probs.view(batch_size * time_steps, voc_size)
        # total_loss += loss_dict['loss'].data.cpu().tolist()[0]
        total_loss += loss_dict['loss'].data.cpu().tolist()
        # criterion(log_probs_2d, trg_var.view(-1))

        # token accuracy
        correct = predictions.eq(trg_var.data).long()
        mask = trg_var.ne(pad_idx_trg).data.long()
        correct *= mask
        n_correct_batch = correct.cpu().numpy().sum()
        n_correct += n_correct_batch

        # sequence exact match
        cor_per_seq = np.equal(correct.sum(1).cpu().numpy(), trg_lengths)
        n_seqs_correct += cor_per_seq.sum()

        # src tag accuracy
        if predict_src_tags:
            batch_src_tag_correct, batch_src_tag_total = get_accuracy(src_tags_var, src_tag_predictions,
                                                                      pad_idx=pad_idx_tags_src)
            n_src_tags_correct += batch_src_tag_correct.data.cpu().numpy()[0]
            n_src_tags += batch_src_tag_total.data.cpu().numpy()[0]

        # trg tag accuracy
        if predict_trg_tags:
            batch_trg_tag_correct, batch_trg_tag_total = get_accuracy(trg_tags_var, trg_tag_predictions,
                                                                      pad_idx=pad_idx_tags_trg)
            n_trg_tags_correct += batch_trg_tag_correct.data.cpu().numpy()[0]
            n_trg_tags += batch_trg_tag_total.data.cpu().numpy()[0]

    n_correct = n_correct
    n_seqs_correct = n_seqs_correct
    ppx = math.exp(total_loss / n_words)
    acc = 100. * (n_correct / n_words)
    em = 100. * (n_seqs_correct / n_seqs)

    if predict_src_tags:
        logger.info("src tag accuracy: %f (%d/%d)" % (
            100.*n_src_tags_correct/n_src_tags, n_src_tags_correct, n_src_tags))

    if predict_trg_tags:
        logger.info("trg tag accuracy: %f (%d/%d)" % (
            100.*n_trg_tags_correct/n_trg_tags, n_trg_tags_correct, n_trg_tags))

    if use_cuda:
        torch.cuda.empty_cache()
    return total_loss, ppx, acc, em, n_words, n_correct, n_seqs, n_seqs_correct


def get_accuracy(gold_var=None, preds_var=None, pad_idx=None):
    """
    Simply returns n_correct and n_total for a batch of predictions
    Will not take into account where gold_var has pad_index.
    Warning: Includes "</s>"
    """
    assert pad_idx is not None, "set pad index"

    preds_var = preds_var.view(gold_var.size(0), gold_var.size(1))
    correct = preds_var.eq(gold_var).long()
    mask = gold_var.ne(pad_idx).long()
    correct *= mask
    n_correct = correct.sum()
    n_total = mask.sum()
    return n_correct, n_total


def evaluate_exact_match(model=None, batch_iter=None, trg_vocab=None, max_length=0, scan_normalize=False):
    """
    Get exact match score (complete sequence is correct)

    This is the accuracy metric used in Lake & Baroni (2017): "Still not systematic after all these years:
    on the compositional skills of sequence-to-sequence recurrent networks."
    """
    assert max_length > 0, "please specify a maximum length, this is prediction!"

    correct = 0
    total = 0

    pad_index = trg_vocab.stoi[PAD_TOKEN]
    model.eval()

    # special for SCAN
    after_idx = trg_vocab.stoi['after'] if scan_normalize else None
    and_idx = trg_vocab.stoi['and'] if scan_normalize else None
    eos_idx = trg_vocab.stoi[EOS_TOKEN]

    if scan_normalize:
        back_trans = dict()
        with open("/private/home/bastings/git/scan/tasks.txt") as f:
            for line in f:
                line = line[3:].strip()
                src, trg = line.split("OUT: ", 2)
                src = src.strip()
                trg = trg.strip()
                back_trans[src] = trg
        back_trans = defaultdict(lambda: '', back_trans)

    for batch in iter(batch_iter):

        src_var, src_lengths = batch.src
        trg_var, trg_lengths = batch.trg
        src_tags_var = batch.src_tags if hasattr(batch, 'src_tags') else None
        trg_tags_var = batch.trg_tags if hasattr(batch, 'trg_tags') else None

        src_lengths = src_lengths.view(-1).tolist()
        trg_lengths = trg_lengths.view(-1).tolist()

        time_steps = trg_var.size(1)  # longer seqs will never match
        total += len(src_lengths)     # number of sequences

        if scan_normalize:
            time_steps = time_steps + 5

        # predictions at time step t-1 are the inputs for time step t
        # we stop predicting after time_steps since continuing would not result in a match anymore
        result = model(src_var=src_var, src_lengths=src_lengths, max_length=time_steps,
                       src_tags_var=src_tags_var, trg_tags_var=trg_tags_var)

        predictions = result['preds']

        if scan_normalize:

            predictions = predictions.cpu().numpy()
            targets = trg_var.data.cpu().numpy()

            for prediction, target, l in zip(predictions, targets, trg_lengths):

                # if prediction[l-1] != eos_idx:
                #     continue  # length wrong
                eos_es = np.where(prediction == eos_idx)[0]
                if len(eos_es) > 0:
                    prediction = prediction[:eos_es[0]]  # prediction without EOS

                tokens = [trg_vocab.itos[x] for x in prediction]
                prediction_str = ' '.join(tokens)

                tokens = [trg_vocab.itos[x] for x in target[:l-1]]
                target_str = ' '.join(tokens)

                prediction_back = back_trans[prediction_str]
                target_back = back_trans[target_str]

                if prediction_back == target_back:
                    correct += 1

        else:
            # exact match accuracy
            correct_words = predictions.eq(trg_var.data).long()
            mask = (trg_var != pad_index).data.long()
            correct_words *= mask

            n_correct_per_sequence = correct_words.sum(1).cpu().numpy()

            # the sequence is correct when all words are correct including <eos>
            seq_is_exact_match = np.equal(n_correct_per_sequence, trg_lengths)
            correct += seq_is_exact_match.sum()

    em = 100. * (correct / total)

    return em, correct, total


def predict_and_get_bleu(dataset=None, model=None, output_path='output.txt', max_length=50, device=None,
                         debpe=True, trg_path='', src_vocab=None, trg_vocab=None):
    from seq2seq.test import predict_and_save

    logger.info("Translating. This might take a while..")
    single_iter = data.Iterator(dataset=dataset, batch_size=1, train=False, sort=False, device=device)
    predict_and_save(output_path, model=model, batch_iter=single_iter,
                     src_vocab=src_vocab, trg_vocab=trg_vocab, max_length=max_length)

    if debpe:
        cmd = "cat %s | sed 's/@@ //g' > %s.debpe" % (output_path, output_path)
        subprocess.check_output(cmd, shell=True, stderr=None)

        cmd = "cat %s | sed 's/@@ //g' > %s.debpe" % (trg_path, trg_path)
        subprocess.check_output(cmd, shell=True, stderr=None)

        trg_path = trg_path + '.debpe'
        output_path = output_path + '.debpe'

    # (external) BLEU
    # This was used to verify the internal BLEU implementation
    ext_bleu_score = evaluate_bleu_external(ref_path=trg_path, pred_path=output_path)

    return ext_bleu_score


def evaluate_bleu(model=None, batch_iter=None, trg_vocab=None, max_length=0):
    """
    Get (approximate) BLEU score
    This is approximate, because this has unknown words in the reference. Be careful!

    Args:
        model:
        batch_iter:
        trg_vocab:
        max_length: maximum length to decode

    Returns:
        bleu_score: float BLEU score

    """
    assert max_length > 0, "please specify a maximum length, this is prediction!"

    unk_index = trg_vocab.stoi[UNK_TOKEN]
    pad_index = trg_vocab.stoi[PAD_TOKEN]
    eos_index = trg_vocab.stoi[EOS_TOKEN]

    scorer = bleu.Scorer(pad_index, eos_index, unk_index)

    model.eval()

    for batch in iter(batch_iter):

        src_var, src_lengths = batch.src
        trg_var, trg_lengths = batch.trg
        src_tags_var = batch.src_tags if hasattr(batch, 'src_tags') else None
        trg_tags_var = batch.trg_tags if hasattr(batch, 'trg_tags') else None

        src_lengths = src_lengths.view(-1).tolist()
        trg_lengths = trg_lengths.view(-1).tolist()

        # assert len(trg_lengths) == 1, "batch size must be 1"

        # predictions at time step t-1 are the inputs for time step t
        # we stop predicting after time_steps since continuing would not result in a match anymore
        result = model(src_var=src_var, src_lengths=src_lengths, max_length=max_length,
                       src_tags_var=src_tags_var, trg_tags_var=trg_tags_var,
                       return_states=False, return_attention=False)
        predictions = result['preds']
        batch_size = src_var.size(0)

        # make sure every sequence has an <eos> at the end, so that we can use nonzero() safely
        eos_column = LongTensor(batch_size, 1).fill_(eos_index)
        predictions = torch.cat((predictions, eos_column), dim=1)

        for ref, pred in zip(trg_var, predictions):
            # predictions were batches, so may contain multiple <eos> sequences, we want to stop at the first one!
            find_eos = (pred == eos_index).nonzero().view(-1)[0]
            pred = pred[:find_eos+1]
            scorer.add(ref.data.int().cpu(), pred.int().cpu())

    logger.info(scorer.result_string(4))
    bleu_score = scorer.score(order=4)

    return bleu_score


def evaluate_bleu_external(ref_path=None, pred_path=None, multi_bleu_script="seq2seq/lib/multi-bleu.perl"):
    """
    Get external BLEU score
    """
    cmd = "%s %s < %s" % (multi_bleu_script, ref_path, pred_path)
    bleu_output = subprocess.check_output(cmd, shell=True, stderr=None)
    bleu_output = bleu_output.strip().decode('utf-8')
    logger.info(bleu_output)
    m = re.search("BLEU = (.+?),", bleu_output)
    bleu_score = float(m.group(1))
    return bleu_score


def get_symbol_type_cooc_matrix(model=None, batch_iter=None, trg_vocab=None, max_length=0, n_symbols=0):
    """ Get symbol-type co-occurrence matrix. """
    assert max_length > 0, "please specify a maximum length, this is prediction!"
    assert n_symbols > 0, "please specify n_symbols"

    unk_index = trg_vocab.stoi[UNK_TOKEN]
    pad_index = trg_vocab.stoi[PAD_TOKEN]
    eos_index = trg_vocab.stoi[EOS_TOKEN]
    n_words = len(trg_vocab)

    model.eval()

    cooc = np.zeros([n_symbols, n_words])
    sym_counts = np.zeros(n_symbols)

    for batch in iter(batch_iter):

        src_var, src_lengths = batch.src
        trg_var, trg_lengths = batch.trg
        src_tags_var = batch.src_tags if hasattr(batch, 'src_tags') else None
        trg_tags_var = batch.trg_tags if hasattr(batch, 'trg_tags') else None
        max_time = trg_var.size(1)

        src_lengths = src_lengths.view(-1).tolist()
        # trg_lengths = trg_lengths.view(-1).tolist()

        result = model(src_var=src_var, src_lengths=src_lengths, max_length=max_time,
                       src_tags_var=src_tags_var, trg_tags_var=trg_tags_var,
                       return_log_probs=True)

        # predictions = result['preds']
        log_probs = result['log_probs']
        pred_mask = result['mask']
        probs = torch.exp(log_probs)

        # mask padding
        pad_mask = (trg_var == pad_index)
        pad_mask = pad_mask + (torch.eq(pred_mask, False))
        pad_mask.unsqueeze_(2)
        probs = probs.masked_fill(pad_mask, 0.)
        probs = probs.data.cpu().numpy()

        symbols = result['symbols'].data.cpu().numpy()

        # symbol_log_probs = result['symbol_log_probs']
        # make sure every sequence has an <eos> at the end, so that we can use nonzero() safely
        # eos_column = LongTensor(batch_size, 1).fill_(eos_index)
        # predictions = torch.cat((predictions.data, eos_column), dim=1)

        for prob1, sym1 in zip(probs, symbols):
            for probs, sym in zip(prob1, sym1):
                cooc[sym, :] += probs
                sym_counts[sym] += 1

    cooc = cooc / np.maximum(1., sym_counts[:, None])

    return cooc