import torch
import os
import logging
import argparse
from tqdm import tqdm, trange
import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, SequentialSampler
import torch.nn as nn
#from transformers.modeling_bert import BertPreTrainedModel, BertModel, BertConfig
from transformers.models.bert.modeling_bert import BertPreTrainedModel, BertModel, BertConfig
from transformers import BertConfig, BertTokenizer
from torchcrf import CRF
from jointbert import JointBERT

#General config
device = "cuda" if torch.cuda.is_available() else "cpu"

def get_args(pred_config):
    return torch.load(os.path.join(pred_config.model_dir, 'training_args.bin'))

def get_intent_labels(args):
    return [label.strip() for label in open(os.path.join(args.data_dir, args.task, args.intent_label_file), 'r', encoding='utf-8')]

def get_slot_labels(args):
    return [label.strip() for label in open(os.path.join(args.data_dir, args.task, args.slot_label_file), 'r', encoding='utf-8')]


def convert_input_to_tensor(line,
                            pred_config,
                            args,
                            tokenizer,
                            pad_token_label_id,
                            cls_token_segment_id=0,
                            pad_token_segment_id=0,
                            sequence_a_segment_id=0,
                            mask_padding_with_zero=True):
    # Setting based on the current model type
    cls_token = tokenizer.cls_token
    sep_token = tokenizer.sep_token
    unk_token = tokenizer.unk_token
    pad_token_id = tokenizer.pad_token_id

    all_input_ids = []
    all_attention_mask = []
    all_token_type_ids = []
    all_slot_label_mask = []
    tokens = []
    slot_label_mask = []
            
    for word in line:
            word_tokens = tokenizer.tokenize(word)
            if not word_tokens:
                word_tokens = [unk_token]  # For handling the bad-encoded word
            tokens.extend(word_tokens)
            # Use the real label id for the first token of the word, and padding ids for the remaining tokens
            slot_label_mask.extend([pad_token_label_id + 1] + [pad_token_label_id] * (len(word_tokens) - 1))

    # Account for [CLS] and [SEP]
    special_tokens_count = 2
    if len(tokens) > args.max_seq_len - special_tokens_count:
        tokens = tokens[: (args.max_seq_len - special_tokens_count)]
        slot_label_mask = slot_label_mask[:(args.max_seq_len - special_tokens_count)]

    # Add [SEP] token
    tokens += [sep_token]
    token_type_ids = [sequence_a_segment_id] * len(tokens)
    slot_label_mask += [pad_token_label_id]

    # Add [CLS] token
    tokens = [cls_token] + tokens
    token_type_ids = [cls_token_segment_id] + token_type_ids
    slot_label_mask = [pad_token_label_id] + slot_label_mask

    input_ids = tokenizer.convert_tokens_to_ids(tokens)

    # The mask has 1 for real tokens and 0 for padding tokens. Only real tokens are attended to.
    attention_mask = [1 if mask_padding_with_zero else 0] * len(input_ids)

    # Zero-pad up to the sequence length.
    padding_length = args.max_seq_len - len(input_ids)
    input_ids = input_ids + ([pad_token_id] * padding_length)
    attention_mask = attention_mask + ([0 if mask_padding_with_zero else 1] * padding_length)
    token_type_ids = token_type_ids + ([pad_token_segment_id] * padding_length)
    slot_label_mask = slot_label_mask + ([pad_token_label_id] * padding_length)

    all_input_ids.append(input_ids)
    all_attention_mask.append(attention_mask)
    all_token_type_ids.append(token_type_ids)
    all_slot_label_mask.append(slot_label_mask)

    # Change to Tensor
    all_input_ids = torch.tensor(all_input_ids, dtype=torch.long)
    all_attention_mask = torch.tensor(all_attention_mask, dtype=torch.long)
    all_token_type_ids = torch.tensor(all_token_type_ids, dtype=torch.long)
    all_slot_label_mask = torch.tensor(all_slot_label_mask, dtype=torch.long)

    dataset = TensorDataset(all_input_ids, all_attention_mask, all_token_type_ids, all_slot_label_mask)

    return dataset
    
def predict(pred_config):
    #Define model
    args = get_args(pred_config)
    model = JointBERT.from_pretrained(args.model_dir,
                                      args=args,
                                      intent_label_lst=get_intent_labels(args),
                                      slot_label_lst=get_slot_labels(args))
    model.to(device)
    model.eval()
    #Define intent and slots labels
    intent_label_lst = get_intent_labels(args)
    slot_label_lst = get_slot_labels(args)
    #Define tokenizer
    tokenizer = BertTokenizer.from_pretrained(args.model_name_or_path)
    #Define input
    input_text = 'si pagare'
    #Encode input
    pad_token_label_id = args.ignore_index
    encoded_input = convert_input_to_tensor(input_text, pred_config, args, tokenizer, pad_token_label_id)
    #Predict
    sampler = SequentialSampler(encoded_input)
    data_loader = DataLoader(encoded_input, sampler=sampler, batch_size=pred_config.batch_size)

    all_slot_label_mask = None
    intent_preds = None
    slot_preds = None

    for batch in tqdm(data_loader, desc="Predicting"):
        batch = tuple(t.to(device) for t in batch)
        with torch.no_grad():
            inputs = {"input_ids": batch[0],
                      "attention_mask": batch[1],
                      "intent_label_ids": None,
                      "slot_labels_ids": None}
            if args.model_type != "distilbert":
                inputs["token_type_ids"] = batch[2]
            outputs = model(**inputs)
            _, (intent_logits, slot_logits) = outputs[:2]

            # Intent Prediction
            if intent_preds is None:
                intent_preds = intent_logits.detach().cpu().numpy()
            else:
                intent_preds = np.append(intent_preds, intent_logits.detach().cpu().numpy(), axis=0)

            # Slot prediction
            if slot_preds is None:
                if args.use_crf:
                    # decode() in `torchcrf` returns list with best index directly
                    slot_preds = np.array(model.crf.decode(slot_logits))
                else:
                    slot_preds = slot_logits.detach().cpu().numpy()
                all_slot_label_mask = batch[3].detach().cpu().numpy()
            else:
                if args.use_crf:
                    slot_preds = np.append(slot_preds, np.array(model.crf.decode(slot_logits)), axis=0)
                else:
                    slot_preds = np.append(slot_preds, slot_logits.detach().cpu().numpy(), axis=0)
                all_slot_label_mask = np.append(all_slot_label_mask, batch[3].detach().cpu().numpy(), axis=0)

    intent_preds = np.argmax(intent_preds, axis=1)

    if not args.use_crf:
        slot_preds = np.argmax(slot_preds, axis=2)
    
    slot_label_map = {i: label for i, label in enumerate(slot_label_lst)}
    slot_preds_list = [[] for _ in range(slot_preds.shape[0])]    

    for i in range(slot_preds.shape[0]):
            for j in range(slot_preds.shape[1]):
                if all_slot_label_mask[i, j] != pad_token_label_id:
                    print(f'slot_preds_list: {slot_preds_list}')
                    print(f'slot_label_map: {slot_label_map}')
                    print(f'slot_preds: {slot_preds}')
                    #print(f'slot_preds[i][j]: {slot_preds[i][j]}')
                    aux = slot_label_map[slot_preds[i][j]]
                    print(f'aux :{aux}')
                    slot_preds_list[i].append(aux)
        
    print(f'Intent preds: {intent_preds}')
    print(f'slott preds: {slot_preds}')
    for words, slot_preds, intent_pred in zip(input_text, slot_preds_list, intent_preds):
            line = ""
            for word, pred in zip(words, slot_preds):
                if pred == 'O':
                    line = line + word + " "
                else:
                    line = line + "[{}:{}] ".format(word, pred)
            print("<{}> -> {}\n".format(intent_label_lst[intent_pred], line.strip()))


    
if __name__ == "__main__":
    #init_logger()
    parser = argparse.ArgumentParser()

    parser.add_argument("--input_file", default="sample_pred_in.txt", type=str, help="Input file for prediction")
    parser.add_argument("--output_file", default="sample_pred_out.txt", type=str, help="Output file for prediction")
    parser.add_argument("--model_dir", default="./smartia_model", type=str, help="Path to save, load model")

    parser.add_argument("--batch_size", default=32, type=int, help="Batch size for prediction")
    parser.add_argument("--no_cuda", action="store_true", help="Avoid using CUDA when available")

    pred_config = parser.parse_args()
    predict(pred_config)