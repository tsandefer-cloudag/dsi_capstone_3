import numpy as np
import re

from keras.models import Model
from keras.layers import Input, LSTM, Dense, Bidirectional, Concatenate
from keras.callbacks import ModelCheckpoint

class AnnotationGenerator(object):
    '''
    Character-based seq2seq model that uses pretrained weights to allow a user to pass in lyrics to be annotated/explained
    '''

    def __init__(self, trained_model='adam_100ep_256ld',
                        models_filepath='./models/',
                        data_filepath='./data/',
                        final_weights_fp='_weights',
                        data_name='data',
                        use_bidirectional=False,
                        latent_dim=256,
                        temp=1,
                        start_char='\v',
                        end_char='\b'):

        self.trained_model = trained_model
        self.data_filepath = data_filepath
        self.models_filepath = models_filepath
        self.data_name = data_name
        self.final_weights_fp = final_weights_fp

        self.use_bidirectional = use_bidirectional

        self.latent_dim = latent_dim # 256 or 512
        self.temp = temp # for sampling w/ diversity

        self.start_char = start_char
        self.end_char = end_char

        # load in model feature values
        self.text_stats = np.load(f'{self.data_filepath}{self.data_name}_text_stats.npy').item()
        self.num_encoder_tokens = self.text_stats['num_encoder_tokens']
        self.num_decoder_tokens = self.text_stats['num_decoder_tokens']
        self.max_encoder_seq_length = self.text_stats['max_encoder_seq_length']
        self.max_decoder_seq_length = self.text_stats['max_decoder_seq_length']

        # load in char to idx dictionary values
        self.input_token_index = np.load(f'{self.data_filepath}{self.data_name}_input_token_index.npy').item()
        self.reverse_input_char_index = np.load(f'{self.data_filepath}{self.data_name}_reverse_input_char_index.npy').item()
        self.target_token_index = np.load(f'{self.data_filepath}{self.data_name}_target_token_index.npy').item()
        self.reverse_target_char_index = np.load(f'{self.data_filepath}{self.data_name}_reverse_target_char_index.npy').item()

        # Define encoder model input and LSTM layers and states exactly as defined in training model
        encoder_inputs = Input(shape=(None, self.num_encoder_tokens), name='encoder_inputs')
        if self.use_bidirectional:
            encoder = Bidirectional(LSTM(self.latent_dim, return_state=True, name='encoder_LSTM')) #, merge_mode='concat')
            encoder_outputs, forward_h, forward_c, backward_h, backward_c = encoder(encoder_inputs)

            # discard 'encoder_outputs' and only keep the h anc c states.
            state_h = Concatenate()([forward_h, backward_h])
            state_c = Concatenate()([forward_c, backward_c])
        else:
            encoder = LSTM(self.latent_dim, return_state=True, name='encoder_LSTM')
            encoder_outputs, state_h, state_c = encoder(encoder_inputs)

        encoder_states = [state_h, state_c]

        decoder_lstm_dim = self.latent_dim * 2 if self.use_bidirectional else self.latent_dim


        decoder_inputs = Input(shape=(None, self.num_decoder_tokens), name='decoder_inputs')
        decoder_lstm = LSTM(decoder_lstm_dim, return_sequences=True, return_state=True, name='decoder_lstm')
        decoder_outputs, _, _ = decoder_lstm(decoder_inputs, initial_state=encoder_states)
        decoder_dense = Dense(self.num_decoder_tokens, activation='softmax', name='decoder_dense')
        decoder_outputs = decoder_dense(decoder_outputs)

        self.model = Model([encoder_inputs, decoder_inputs], decoder_outputs)
        self.model.load_weights(f'{self.models_filepath}{self.trained_model}{self.final_weights_fp}.h5')

        # create encoder and decoder models for prediction
        self.encoder_model = Model(encoder_inputs, encoder_states)
        decoder_state_input_h = Input(shape=(self.latent_dim,))
        decoder_state_input_c = Input(shape=(self.latent_dim,))
        decoder_states_inputs = [decoder_state_input_h, decoder_state_input_c]
        decoder_outputs, state_h, state_c = decoder_lstm(decoder_inputs, initial_state=decoder_states_inputs)
        decoder_states = [state_h, state_c]
        decoder_outputs = decoder_dense(decoder_outputs)
        self.decoder_model = Model([decoder_inputs] + decoder_states_inputs, [decoder_outputs] + decoder_states)

    def _clean_text(self, txt):
        '''
        For now, data is small enough for regex to still be reasonable... Let's go with this for now
        '''
        txt = txt.lower().strip()
        # clean_txt_lines[idx] = ''.join([i for i in line if i in chars_to_keep_lst])
        txt = re.sub(r"[^a-zA-Z?.!, ';:#$@&%*-+=\n\d+]", "", txt, re.M)
        # collapses multiple spaces into just one space
        txt = re.sub(r'(  +)', " ", txt, re.M)
        # line = self.start_token + line + self.end_token
        return txt

    def _encode_input_sentence(self, sentence):
        sentence = self._clean_text(sentence)
        if len(sentence) > self.max_encoder_seq_length:
            sentence = sentence[:self.max_encoder_seq_length]
        encoder_input_sent = np.zeros((1,
                                       self.max_encoder_seq_length,
                                       self.num_encoder_tokens),
                                       dtype='float32')
        for t, char in enumerate(sentence):
            encoder_input_sent[0, t, self.input_token_index[char]] = 1
        return encoder_input_sent

    def _sample_with_diversity(self, preds, temperature=None):
        temperature = temperature if temperature else self.temp
        # same as Erin's function
        preds = np.asarray(preds).astype('float64')
        preds = np.log(preds) / temperature
        exp_preds = np.exp(preds)
        preds = exp_preds / np.sum(exp_preds)
        probas = np.random.multinomial(1, preds, 1)
        return np.argmax(probas)

    def reply(self, sentence, diversity=False, temp=None):
        self.encoder_input_sent = self._encode_input_sentence(sentence)
        # Encode the input as state vectors.
        states_value = self.encoder_model.predict(self.encoder_input_sent)
        # Generate empty target sequence of length 1.
        target_seq = np.zeros((1, 1, self.num_decoder_tokens))
        # Populate the first character of target sequence with the start character.
        target_seq[0, 0, self.target_token_index[self.start_char]] = 1.
        # Sampling loop for a batch of sequences
        # (to simplify, here we assume a batch of size 1).
        stop_condition = False
        decoded_sentence = ''
        while not stop_condition:
            output_tokens, h, c = self.decoder_model.predict([target_seq] + states_value)
            if diversity:
                sampled_token_index = self._sample_with_diversity(output_tokens[0, -1, :], temperature=temp)
            else:
                sampled_token_index = np.argmax(output_tokens[0, -1, :])
            sampled_char = self.reverse_target_char_index[sampled_token_index]
            decoded_sentence += sampled_char
            # Exit condition: either hit max length or find stop character.
            if (sampled_char == self.end_char or len(decoded_sentence) > self.max_decoder_seq_length):
                stop_condition = True
            # Update the target sequence (of length 1).
            target_seq = np.zeros((1, 1, self.num_decoder_tokens))
            target_seq[0, 0, sampled_token_index] = 1
            # Update states
            states_value = [h, c]
        decoded_sentence = decoded_sentence.replace('nigga', 'n****')
        return decoded_sentence

    def test_run(self, chat=False):
        input_sentence = "she callin', she textin', she’s fallin', but let me explain"

        print(f"\n\nInput Sentence: {input_sentence}")
        print(f"Reply: {self.reply(input_sentence)}")

def main():
    model = AnnotationGenerator(trained_model='bidir_adam_100ep_512ld',
                        models_filepath='./models/',
                        data_filepath='./data/',
                        final_weights_fp='_weights',
                        data_name='data',
                        use_bidirectional=True,
                        latent_dim=512,
                        temp=1,
                        start_char='\v',
                        end_char='\b')

    model.test_run(chat=True)

if __name__ == "__main__":
    main()