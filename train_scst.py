#!/usr/bin/env python3
import os
import random
import argparse
import logging
import numpy as np
from tensorboardX import SummaryWriter

import high_level_cornell, model, utils

import torch
import torch.optim as optim
import torch.nn.functional as F

import ptan

SAVES_DIR = "saves"

BATCH_SIZE = 16
#GPU requirements for SCST are higher, so a smaller alpha.
LEARNING_RATE = 1e-4
MAX_EPOCHES = 5000

log = logging.getLogger("train")


def run_test(test_data, net, end_token, device="cpu"):
    """
    While training on every epoch we calculate BLEU for test dataset.
    Shape of the data is [(phrase,[responses])].
    We strip the #BEG token as well.
    """
    bleu_sum = 0.0
    bleu_count = 0
    for p1, p2 in test_data:
        input_seq = model.pack_input(p1, net.emb, device)
        enc = net.encode(input_seq)
        _, tokens = net.decode_chain_argmax(enc, input_seq.data[0:1], seq_len=high_level_cornell.MAX_TOKENS,
                                            stop_token=end_token)
        ref_indices = [
            indices[1:]
            for indices in p2
        ]
        bleu_sum += utils.calc_bleu_many(tokens, ref_indices)
        bleu_count += 1
    return bleu_sum / bleu_count


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s", level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="Category to use for training. Empty string to train on full dataset")
    parser.add_argument("--cuda", action='store_true', default=False, help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    #Load pretrained Cross-entropy model to continue RL training from that point.
    parser.add_argument("-l", "--load", required=True, help="Load model and continue in RL mode")
    #Number of samples taken in each epoch. More the no., faster the PG will converge but increases GPU.
    parser.add_argument("--samples", type=int, default=4, help="Count of samples in prob mode")
    #Disabling the skips disables skipping of training samples with high BLEU score.
    parser.add_argument("--disable-skip", default=False, action='store_true', help="Disable skipping of samples with high argmax BLEU")
    args = parser.parse_args()
    device = torch.device("cuda" if args.cuda else "cpu")

    saves_path = os.path.join(SAVES_DIR, args.name)
    os.makedirs(saves_path, exist_ok=True)

    phrase_pairs, emb_dict = high_level_cornell.load_data(genre_filter=args.data)
    log.info("Obtained %d phrase pairs with %d uniq words", len(phrase_pairs), len(emb_dict))
    high_level_cornell.save_emb_dict(saves_path, emb_dict)
    end_token = emb_dict[high_level_cornell.END_TOKEN]
    train_data = high_level_cornell.encode_phrase_pairs(phrase_pairs, emb_dict)
    rand = np.random.RandomState(high_level_cornell.SHUFFLE_SEED)
    rand.shuffle(train_data)
    train_data, test_data = high_level_cornell.split_train_test(train_data)
    log.info("Training data converted, got %d samples", len(train_data))
    train_data = high_level_cornell.group_train_data(train_data)
    test_data = high_level_cornell.group_train_data(test_data)
    log.info("Train set has %d phrases, test %d", len(train_data), len(test_data))

    rev_emb_dict = {idx: word for word, idx in emb_dict.items()}

    net = model.PhraseModel(emb_size=model.EMBEDDING_DIM, dict_size=len(emb_dict),
                            hid_size=model.HIDDEN_STATE_SIZE).to(device)
    log.info("Model: %s", net)

    writer = SummaryWriter(comment="-" + args.name)
    net.load_state_dict(torch.load(args.load))
    log.info("Model loaded from %s, continue training in RL mode...", args.load)

    # BEGIN token
    beg_token = torch.LongTensor([emb_dict[high_level_cornell.BEGIN_TOKEN]]).to(device)

    with ptan.common.utils.TBMeanTracker(writer, batch_size=100) as tb_tracker:
        optimiser = optim.Adam(net.parameters(), lr=LEARNING_RATE, eps=1e-3)
        batch_idx = 0
        best_bleu = None
        for epoch in range(MAX_EPOCHES):
            random.shuffle(train_data)
            dial_shown = False
            #We keep track of total amt of samples.
            total_samples = 0
            #Also, count the skipped samples due to high BLEU score
            skipped_samples = 0
            bleus_argmax = []
            bleus_sample = []

            for batch in high_level_cornell.iterate_batches(train_data, BATCH_SIZE):
                batch_idx += 1
                optimiser.zero_grad()
                input_seq, input_batch, output_batch = model.pack_batch_no_out(batch, net.emb, device)
                enc = net.encode(input_seq)

                net_policies = []
                net_actions = []
                net_advantages = []
                beg_embedding = net.emb(beg_token)

                for idx, inp_idx in enumerate(input_batch):
                    total_samples += 1
                    ref_indices = [
                        indices[1:]
                        for indices in output_batch[idx]
                    ]
                    item_enc = net.get_encoded_item(enc, idx)
                    r_argmax, actions = net.decode_chain_argmax(item_enc, beg_embedding, high_level_cornell.MAX_TOKENS,
                                                                stop_token=end_token)

                    argmax_bleu = utils.calc_bleu_many(actions, ref_indices)
                    #This will be used as baseline for our PG algorithm.
                    bleus_argmax.append(argmax_bleu)

                    if not args.disable_skip and argmax_bleu > 0.99:
                        skipped_samples += 1
                        continue

                    if not dial_shown:
                        #Input Phrase.
                        log.info("Input: %s", utils.untokenize(high_level_cornell.decode_words(inp_idx, rev_emb_dict)))
                        #Our reference sentence.
                        ref_words = [utils.untokenize(high_level_cornell.decode_words(ref, rev_emb_dict)) for ref in ref_indices]
                        log.info("Refer: %s", " ~~|~~ ".join(ref_words))
                        log.info("Argmax: %s, bleu=%.4f", utils.untokenize(high_level_cornell.decode_words(actions, rev_emb_dict)),
                                 argmax_bleu)

                    for _ in range(args.samples):
                        r_sample, actions = net.decode_chain_sampling(item_enc, beg_embedding,
                                                                      high_level_cornell.MAX_TOKENS, stop_token=end_token)
                        #Obtaining bleu of samples.
                        sample_bleu = utils.calc_bleu_many(actions, ref_indices)

                        if not dial_shown:
                            log.info("Sample: %s, bleu=%.4f", utils.untokenize(high_level_cornell.decode_words(actions, rev_emb_dict)),
                                     sample_bleu)

                        net_policies.append(r_sample)
                        net_actions.extend(actions)
                        net_advantages.extend([sample_bleu - argmax_bleu] * len(actions))
                        bleus_sample.append(sample_bleu)
                    dial_shown = True

                if not net_policies:
                    continue

                policies_v = torch.cat(net_policies)
                actions_t = torch.LongTensor(net_actions).to(device)
                adv_v = torch.FloatTensor(net_advantages).to(device)
                log_prob_v = F.log_softmax(policies_v, dim=1)
                log_prob_actions_v = adv_v * log_prob_v[range(len(net_actions)), actions_t]
                loss_policy_v = -log_prob_actions_v.mean()

                loss_v = loss_policy_v
                loss_v.backward()
                optimiser.step()

                tb_tracker.track("advantage", adv_v, batch_idx)
                tb_tracker.track("loss_policy", loss_policy_v, batch_idx)
                tb_tracker.track("loss_total", loss_v, batch_idx)

            bleu_test = run_test(test_data, net, end_token, device)
            bleu = np.mean(bleus_argmax)
            writer.add_scalar("bleu_test", bleu_test, batch_idx)
            writer.add_scalar("bleu_argmax", bleu, batch_idx)
            writer.add_scalar("bleu_sample", np.mean(bleus_sample), batch_idx)
            writer.add_scalar("skipped_samples", skipped_samples / total_samples, batch_idx)
            writer.add_scalar("epoch", batch_idx, epoch)
            log.info("Epoch %d, test BLEU: %.3f", epoch, bleu_test)
            if best_bleu is None or best_bleu < bleu_test:
                best_bleu = bleu_test
                log.info("Best bleu updated: %.4f", bleu_test)
                torch.save(net.state_dict(), os.path.join(saves_path, "bleu_%.3f_%02d.dat" % (bleu_test, epoch)))
            if epoch % 10 == 0:
                torch.save(net.state_dict(), os.path.join(saves_path, "epoch_%03d_%.3f_%.3f.dat" % (epoch, bleu, bleu_test)))

    writer.close()
