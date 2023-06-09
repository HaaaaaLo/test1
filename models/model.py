# -*- coding: utf-8 -*-
# !/usr/bin/python

"""
# @File    : model.py
# @Software: PyCharm
"""

import sys
import copy
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

sys.path.append("..")
from utils.embedding import Embeddings
from models.rnn import LSTM
from models.gnn import GraphTransformer
from models.attention import dot_prod_attention
from models.pointer_net import PointerNet
from utils.utils import identity, pad_tensor_1d, mk_graph_for_gnn, length_array_to_mask_tensor
from rules.grammar import AbstractQueryGraph

V_CLASS_NUM = 5
E_CLASS_NUM = 5


class AQGNet(nn.Module):

    def __init__(self, vocab, args):

        super(AQGNet, self).__init__()
        self.args = args

        wo_vocab = vocab
        self.word_embedding = Embeddings(args.d_emb, wo_vocab)
        self.vertex_embedding = nn.Embedding(V_CLASS_NUM, args.d_h)
        self.edge_embedding = nn.Embedding(E_CLASS_NUM, args.d_h)

        if args.cuda:
            self.new_long_tensor = torch.cuda.LongTensor
            self.new_tensor = torch.cuda.FloatTensor
        else:
            self.new_long_tensor = torch.LongTensor
            self.new_tensor = torch.FloatTensor

        self.encoder_lstm = LSTM(d_input=args.d_emb, d_h=args.d_h // 2,
                                n_layers=args.n_lstm_layers, birnn=args.birnn, dropout=args.dropout)

        self.decoder_lstm = nn.LSTMCell(args.d_h, args.d_h)

        self.decoder_cell_init = nn.Linear(args.d_h, args.d_h)

        self.enc_att_linear = nn.Linear(args.d_h, args.d_h)

        self.dec_input_linear = nn.Linear(args.d_h + args.d_h, args.d_h, bias=False)

        self.dropout = nn.Dropout(args.dropout)

        # encoder for graphs
        self.graph_encoder = GraphTransformer(n_blocks=args.n_gnn_blocks,
                                              hidden_size=args.d_h, dropout=args.dropout)

        self.read_out_active = torch.tanh if args.readout == 'non_linear' else identity

        self.query_vec_to_av_vec = nn.Linear(args.d_h, args.d_h,
                                               bias = args.readout == 'non_linear')
        self.query_vec_to_ae_vec = nn.Linear(args.d_h, args.d_h,
                                               bias=args.readout == 'non_linear')
        self.av_readout_b = nn.Parameter(torch.FloatTensor(V_CLASS_NUM).zero_())
        self.ae_readout_b = nn.Parameter(torch.FloatTensor(E_CLASS_NUM).zero_())

        self.av_readout = lambda q: F.linear(self.read_out_active(self.query_vec_to_av_vec(q)),
                                                     self.vertex_embedding.weight, self.av_readout_b)
        self.ae_readout = lambda q: F.linear(self.read_out_active(self.query_vec_to_ae_vec(q)),
                                                     self.edge_embedding.weight, self.ae_readout_b)

        self.sv_pointer_net = PointerNet(args.d_h, args.d_h, attention_type=args.att_type)

    def forward(self, batch):
        q, q_lens, gold_graphs, gold_objs = batch
        # encoding.
        q_encodings, (enc_h_last, enc_cell_last), q_embeds = self.encode(q, q_lens)
        q_encodings = self.dropout(q_encodings)

        q_mask = length_array_to_mask_tensor(q_lens, self.args.cuda)

        enc_cell_last = torch.cat([enc_cell_last[0], enc_cell_last[1]], -1)

        dec_init_state = self.init_decoder_state(enc_cell_last)
        h_last = dec_init_state

        zero_graph_encoding = Variable(self.new_tensor(self.args.d_h).zero_())

        batch_size = len(q)
        max_op_num = max([len(x) for x in gold_graphs])

        scores = [[] for _ in range(batch_size)]
        action_probs = [[] for _ in range(batch_size)]

        for t in range(max_op_num):

            graph_encodings = []
            vertex_encodings = []

            for s_id in range(batch_size):
                assert len(gold_graphs[s_id]) == len(gold_objs[s_id])

                if t < len(gold_graphs[s_id]):
                    vertices, edges, adj = gold_graphs[s_id][t]

                    vertex_embed = self.vertex_embedding(vertices)
                    edge_embed = self.edge_embedding(edges)

                    vertex_encoding, edge_encoding, \
                    graph_encoding = self.encode_graph(vertex_embed, edge_embed, adj)

                else:
                    graph_encoding = zero_graph_encoding
                    zero_vertex_encoding = Variable(self.new_tensor((t-2) // 3 + 2, self.args.d_h).zero_())
                    vertex_encoding = zero_vertex_encoding

                graph_encodings.append(graph_encoding)
                vertex_encodings.append(vertex_encoding)

            graph_encodings = torch.stack(graph_encodings)
            vertex_encodings = torch.stack(vertex_encodings)

            (h_t, cell_t), ctx = self.decode_step(h_last, q_encodings, graph_encodings,
                                                    src_token_mask=q_mask,
                                                    return_att_weight=True)

            if t == 0 or t % 3 == 1:
                action_prob = self.av_readout(h_t)
            elif t % 3 == 0:
                action_prob = self.ae_readout(h_t)
            else:
                action_prob = self.sv_pointer_net(src_encodings=vertex_encodings, query_vec=h_t.unsqueeze(0),
                                                  src_token_mask=None)

                sv_mask = torch.cat([self.new_long_tensor(action_prob.size(0), action_prob.size(1) - 1).fill_(1),
                                     self.new_long_tensor(action_prob.size(0), 1).zero_()], -1)
                action_prob.masked_fill_(sv_mask == 0, -float('inf'))

            for s_id in range(batch_size):
                if t < len(gold_objs[s_id]):
                    action_probs[s_id].append(action_prob[s_id])

            action_prob = F.softmax(action_prob, dim=-1)

            for s_id in range(batch_size):
                if t < len(gold_objs[s_id]):
                    act_prob_t_i = action_prob[s_id, gold_objs[s_id][t]]
                    scores[s_id].append(act_prob_t_i)

            h_last = (h_t, cell_t)

        score = torch.stack(
            [torch.stack(score_i, dim=0).log().sum() for score_i in scores], dim=0)

        return -score, action_probs

    def generation(self, sample, beam_size=5):
        q, q_lens = sample[:2]

        batch_size = len(q)
        assert batch_size == 1

        # encoding.
        q_encodings, (enc_h_last, enc_cell_last), q_embeds = self.encode(q, q_lens)
        q_encodings = self.dropout(q_encodings)

        q_mask = length_array_to_mask_tensor(q_lens, self.args.cuda)

        enc_cell_last = torch.cat([enc_cell_last[0], enc_cell_last[1]], -1)

        dec_init_state = self.init_decoder_state(enc_cell_last)
        h_last = dec_init_state

        t = 0
        aqg = AbstractQueryGraph()
        aqg.init_state()
        beams = [aqg]
        completed_beams = []

        action_probs = []
        while len(completed_beams) < beam_size and t < self.args.max_num_op:

            exp_q_encodings = q_encodings.expand(len(beams), q_encodings.size(1), q_encodings.size(2))

            graph_encodings = []
            vertex_encodings = []
            for e_id, aqg in enumerate(beams):
                vertices, v_labels, edges = aqg.get_state()

                vertices, edges, adj = mk_graph_for_gnn(vertices, v_labels, edges)

                if self.args.cuda:
                    vertices = vertices.to(self.args.gpu)
                    edges = edges.to(self.args.gpu)
                    adj = adj.to(self.args.gpu)

                vertex_embed = self.vertex_embedding(vertices)
                edge_embed = self.edge_embedding(edges)

                vertex_encoding, edge_encoding, \
                graph_encoding = self.encode_graph(vertex_embed, edge_embed, adj)

                graph_encodings.append(graph_encoding)
                vertex_encodings.append(vertex_encoding)

            graph_encodings = torch.stack(graph_encodings)
            vertex_encodings = torch.stack(vertex_encodings)

            (h_t, cell_t), ctx = self.decode_step(h_last, exp_q_encodings, graph_encodings,
                                                    src_token_mask=q_mask,
                                                    return_att_weight=True)

            if t == 0 or t % 3 == 1:
                op = 'av'
                action_prob = self.av_readout(h_t)
            elif t % 3 == 0:
                op = 'ae'
                action_prob = self.ae_readout(h_t)
            else:
                op = 'sv'
                action_prob = self.sv_pointer_net(src_encodings=vertex_encodings, query_vec=h_t.unsqueeze(0),
                                                  src_token_mask=None)
                sv_mask = torch.cat([self.new_long_tensor(action_prob.size(0), action_prob.size(1) - 1).fill_(1),
                                     self.new_long_tensor(action_prob.size(0), 1).zero_()], -1)
                action_prob.masked_fill_(sv_mask == 0, -float('inf'))

            # for e_id in range(len(beams)):
            #     action_probs[e_id].append(action_prob[e_id])
            action_prob = F.log_softmax(action_prob, dim=-1)

            new_aqg_meta = []
            for e_id, aqg in enumerate(beams):
                obj_range = aqg.get_obj_range(op)
                for o_id in obj_range:
                    obj_score = action_prob[e_id, o_id]
                    new_aqg_score = aqg.score + obj_score.data.cpu()
                    meta_entry = {
                        "op": op,
                        "obj": o_id,
                        "obj_score":obj_score,
                        "new_aqg_score": new_aqg_score,
                        "prev_aqg_id": e_id
                    }
                    new_aqg_meta.append(meta_entry)

            if not new_aqg_meta:
                break

            new_aqg_scores = torch.stack([x['new_aqg_score'] for x in new_aqg_meta], dim=0)

            if t == 0:
                k = 1
            else:
                k = min(new_aqg_scores.size(0), beam_size - len(completed_beams))
            top_new_aqg_scores, meta_ids = torch.topk(new_aqg_scores, k=k)

            live_aqg_ids = []
            new_beams = []
            for new_aqg_score, meta_id in zip(top_new_aqg_scores.data.cpu(), meta_ids.data.cpu()):
                aqg_meta_entry = new_aqg_meta[meta_id]
                op = aqg_meta_entry["op"]
                obj = aqg_meta_entry["obj"]
                new_aqg_score = aqg_meta_entry["new_aqg_score"]
                prev_aqg_id = aqg_meta_entry['prev_aqg_id']
                prev_aqg = beams[prev_aqg_id]

                new_aqg = copy.deepcopy(prev_aqg)

                if op == "av" and obj == V_CLASS_NUM - 1:
                    new_aqg.update_score(new_aqg_score)
                    completed_beams.append(new_aqg)
                else:
                    new_aqg.update_state(op, obj)
                    new_aqg.update_score(new_aqg_score)
                    new_beams.append(new_aqg)
                    live_aqg_ids.append(prev_aqg_id)

            if not live_aqg_ids:
                break

            h_last = (h_t[live_aqg_ids], cell_t[live_aqg_ids])
            beams = new_beams

            t += 1

        completed_beams.sort(key=lambda aqg: -aqg.score)
        if len(completed_beams) == 0:
            return [[], []]

        return completed_beams, action_probs


    def encode_graph(self, vertex_tensor, edge_tensor, adj):

        return self.graph_encoder(vertex_tensor, edge_tensor, adj)

    def encode(self, src_seq, src_lens):
        """
        encode the source sequence
        :return:
            src_encodings: Variable(batch_size, src_sent_len, hidden_size * 2)
            last_state, last_cell: Variable(batch_size, hidden_size)
        """
        src_embed = self.word_embedding(src_seq)
        src_encodings, final_states = self.encoder_lstm(src_embed, src_lens)

        return src_encodings, final_states, src_embed

    def decode_step(self, h_last, src_encodings, graph_encodings, src_token_mask=None,
             return_att_weight=False):
        # h_t: (batch_size, hidden_size)

        src_encodings_linear = self.enc_att_linear(src_encodings)

        context_t, alpha_t = dot_prod_attention(graph_encodings, src_encodings,
                                            src_encodings_linear, mask=src_token_mask)

        dec_input = torch.tanh(self.dec_input_linear(torch.cat([graph_encodings, context_t], 1)))
        dec_input = self.dropout(dec_input)

        h_t, cell_t = self.decoder_lstm(dec_input, h_last)

        if return_att_weight:
            return (h_t, cell_t), alpha_t
        else:
            return (h_t, cell_t)

    def init_decoder_state(self, enc_last_cell):
        h_0 = self.decoder_cell_init(enc_last_cell)
        h_0 = torch.tanh(h_0)

        return h_0, Variable(self.new_tensor(h_0.size()).zero_())