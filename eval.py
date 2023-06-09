# -*- coding: utf-8 -*-
# !/usr/bin/python

"""
# @File    : eval.py
# @Software: PyCharm
"""

import os
import sys
import time
import json
import glob
import copy
import torch
import pickle

sys.path.append("..")
import pargs
from rules.grammar import AbstractQueryGraph
from data_loaders import GenerationDataLoader
from models.model import AQGNet
from utils.utils import kb_constraint, formalize_aqg, check_aqg


if __name__ == '__main__':
    args = pargs.generation_pargs()

    if not args.cuda:
        args.gpu = -1
    if torch.cuda.is_available() and args.cuda:
        print('\nNote: You are using GPU for evaluation.\n')
        torch.cuda.set_device(args.gpu)
    if torch.cuda.is_available() and not args.cuda:
        print('\nWarning: You have Cuda but do not use it. You are using CPU for evaluation.\n')

    wo_vocab = pickle.load(open(args.wo_vocab, 'rb'))
    print("Load word vocab, size: %d" % len(wo_vocab))

    test_data = pickle.load(open(args.test_data, "rb"))

    test_loader = GenerationDataLoader(args)
    test_loader.load_data(test_data, bs=1, use_small=args.use_small, shuffle=False)
    print("Load valid data from \"%s\"." % (args.test_data))
    print("Test data, batch size: %d, batch number: %d" % (1, test_loader.n_batch))

    model = AQGNet(wo_vocab, args)
    if args.cuda:
        model.cuda()
        print('Shift model to GPU.\n')
    model.load_state_dict(torch.load(args.cpt))
    print("Load checkpoint from \"%s\"." % os.path.abspath(args.cpt))

    query_list = []
    result_list = []
    level_corrects = {}
    level_totals = {}
    n_q_correct, n_q_total = 0, 0
    model.eval()
    for s in test_loader.next_batch():
        data = s[-1][0]

        pred_aqgs, action_probs = model.generation(s[:-1], beam_size=args.beam_size)

        origin_data = copy.deepcopy(data)
        tmp_pred_aqgs = []
        for pred_aqg in pred_aqgs:
            pred_aqg, data = formalize_aqg(pred_aqg, origin_data)
            if check_aqg(pred_aqg):
                tmp_pred_aqgs.append((pred_aqg, data))


        pred_aqgs = [x for x in tmp_pred_aqgs]

        top_pred_aqg = pred_aqgs[0][0]
        is_correct = top_pred_aqg.is_equal(data["gold_aqg"])
        data["pred_aqgs"] = pred_aqgs

        query_list.append(data)

        n_q_correct += is_correct
        n_q_total += 1

    acc = n_q_correct * 100. / n_q_total
    print("\nTotal AQG Accuracy: %.2f" % acc)

    checkpoint_dir = '/'.join(args.cpt.split('/')[:-2])

    results_path = os.path.join(checkpoint_dir, 'results.pkl')
    pickle.dump(query_list, open(results_path, "wb"))
    print("Results save to \"{}\"\n".format(results_path))
