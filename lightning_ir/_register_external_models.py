from .base import CHECKPOINT_MAPPING, POST_LOAD_CALLBACKS, STATE_DICT_KEY_MAPPING, LightningIRModel
from .bi_encoder import BiEncoderConfig
from .models import ColConfig, SpladeConfig, T5CrossEncoderConfig


def _map_colbert_marker_tokens(model: LightningIRModel) -> LightningIRModel:
    config = model.config
    query_token_id = config.vocab_size
    doc_token_id = config.vocab_size + 1
    model.resize_token_embeddings(config.vocab_size + 2, 8)
    embeddings = model.embeddings.word_embeddings.weight.data
    embeddings[query_token_id] = embeddings[1]  # [unused0]
    embeddings[doc_token_id] = embeddings[2]  # [unused1]
    return model


def _map_mono_t5_weights(model: LightningIRModel) -> LightningIRModel:
    # [1176, 6136] true, false
    model.linear.weight.data = model.shared.weight.data[[1176, 6136]]
    return model


def _map_rank_t5_weights(model: LightningIRModel) -> LightningIRModel:
    # 32089 <extra_id_10>
    model.linear.weight.data = model.shared.weight.data[[32089]]
    return model


def _register_external_models():
    CHECKPOINT_MAPPING.update(
        {
            "colbert-ir/colbertv2.0": ColConfig(
                query_length=32, doc_length=184, add_marker_tokens=True, normalize=True
            ),
            "naver/splade-v3": SpladeConfig(),
            "sentence-transformers/msmarco-bert-base-dot-v5": BiEncoderConfig(projection=None, embedding_dim=768),
            "sentence-transformers/msmarco-MiniLM-L-6-v3": BiEncoderConfig(projection=None, embedding_dim=384),
            "castorini/monot5-base-msmarco-10k": T5CrossEncoderConfig(decoder_strategy="mono"),
            "castorini/monot5-base-msmarco": T5CrossEncoderConfig(decoder_strategy="mono"),
            "castorini/monot5-large-msmarco-10k": T5CrossEncoderConfig(decoder_strategy="mono"),
            "castorini/monot5-large-msmarco": T5CrossEncoderConfig(decoder_strategy="mono"),
            "castorini/monot5-3b-msmarco-10k": T5CrossEncoderConfig(decoder_strategy="mono"),
            "castorini/monot5-3b-msmarco": T5CrossEncoderConfig(decoder_strategy="mono"),
            "Soyoung97/RankT5-base": T5CrossEncoderConfig(decoder_strategy="rank"),
            "Soyoung97/RankT5-large": T5CrossEncoderConfig(decoder_strategy="rank"),
            "Soyoung97/RankT5-3b": T5CrossEncoderConfig(decoder_strategy="rank"),
        }
    )
    STATE_DICT_KEY_MAPPING.update(
        {
            "colbert-ir/colbertv2.0": [("linear.weight", "bert.projection.weight")],
            "castorini/monot5-base-msmarco-10k": [(None, "linear.weight")],
            "castorini/monot5-base-msmarco": [(None, "linear.weight")],
            "castorini/monot5-large-msmarco-10k": [(None, "linear.weight")],
            "castorini/monot5-large-msmarco": [(None, "linear.weight")],
            "castorini/monot5-3b-msmarco-10k": [(None, "linear.weight")],
            "castorini/monot5-3b-msmarco": [(None, "linear.weight")],
            "Soyoung97/RankT5-base": [(None, "linear.weight")],
            "Soyoung97/RankT5-large": [(None, "linear.weight")],
            "Soyoung97/RankT5-3b": [(None, "linear.weight")],
        }
    )
    POST_LOAD_CALLBACKS.update(
        {
            "colbert-ir/colbertv2.0": _map_colbert_marker_tokens,
            "castorini/monot5-base-msmarco-10k": _map_mono_t5_weights,
            "castorini/monot5-base-msmarco": _map_mono_t5_weights,
            "castorini/monot5-large-msmarco-10k": _map_mono_t5_weights,
            "castorini/monot5-large-msmarco": _map_mono_t5_weights,
            "castorini/monot5-3b-msmarco-10k": _map_mono_t5_weights,
            "castorini/monot5-3b-msmarco": _map_mono_t5_weights,
            "Soyoung97/RankT5-base": _map_rank_t5_weights,
            "Soyoung97/RankT5-large": _map_rank_t5_weights,
            "Soyoung97/RankT5-3b": _map_rank_t5_weights,
        }
    )