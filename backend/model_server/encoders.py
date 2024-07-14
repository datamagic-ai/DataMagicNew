import gc
import json
from typing import Any
from typing import Optional

import openai
import vertexai  # type: ignore
import voyageai  # type: ignore
from cohere import Client as CohereClient
from fastapi import APIRouter
from fastapi import HTTPException
from google.oauth2 import service_account
from sentence_transformers import CrossEncoder  # type: ignore
from sentence_transformers import SentenceTransformer  # type: ignore
from vertexai.language_models import TextEmbeddingInput  # type: ignore
from vertexai.language_models import TextEmbeddingModel  # type: ignore

from danswer.search.enums import EmbedTextType
from danswer.utils.logger import setup_logger
from model_server.constants import DEFAULT_COHERE_MODEL
from model_server.constants import DEFAULT_OPENAI_MODEL
from model_server.constants import DEFAULT_VERTEX_MODEL
from model_server.constants import DEFAULT_VOYAGE_MODEL
from model_server.constants import EmbeddingModelTextType
from model_server.constants import EmbeddingProvider
from model_server.constants import MODEL_WARM_UP_STRING
from model_server.utils import simple_log_function_time
from shared_configs.configs import CROSS_EMBED_CONTEXT_SIZE
from shared_configs.configs import CROSS_ENCODER_MODEL_ENSEMBLE
from shared_configs.configs import INDEXING_ONLY
from shared_configs.model_server_models import EmbedRequest
from shared_configs.model_server_models import EmbedResponse
from shared_configs.model_server_models import RerankRequest
from shared_configs.model_server_models import RerankResponse


logger = setup_logger()

router = APIRouter(prefix="/encoder")

_GLOBAL_MODELS_DICT: dict[str, "SentenceTransformer"] = {}
_RERANK_MODELS: Optional[list["CrossEncoder"]] = None


class CloudEmbedding:
    def __init__(self, api_key: str, provider: str, model: str | None = None):
        self.api_key = api_key

        # Only for Google as is needed on client setup
        self.model = model
        try:
            self.provider = EmbeddingProvider(provider.lower())
        except ValueError:
            raise ValueError(f"Unsupported provider: {provider}")
        self.client = self._initialize_client()

    def _initialize_client(self) -> Any:
        if self.provider == EmbeddingProvider.OPENAI:
            return openai.OpenAI(api_key=self.api_key)
        elif self.provider == EmbeddingProvider.COHERE:
            return CohereClient(api_key=self.api_key)
        elif self.provider == EmbeddingProvider.VOYAGE:
            return voyageai.Client(api_key=self.api_key)
        elif self.provider == EmbeddingProvider.GOOGLE:
            credentials = service_account.Credentials.from_service_account_info(
                json.loads(self.api_key)
            )
            project_id = json.loads(self.api_key)["project_id"]
            vertexai.init(project=project_id, credentials=credentials)
            return TextEmbeddingModel.from_pretrained(
                self.model or DEFAULT_VERTEX_MODEL
            )

        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    def encode(
        self, texts: list[str], model_name: str | None, text_type: EmbedTextType
    ) -> list[list[float]]:
        return [
            self.embed(text=text, text_type=text_type, model=model_name)
            for text in texts
        ]

    def embed(
        self, *, text: str, text_type: EmbedTextType, model: str | None = None
    ) -> list[float]:
        logger.debug(f"Embedding text with provider: {self.provider}")
        if self.provider == EmbeddingProvider.OPENAI:
            return self._embed_openai(text, model)

        embedding_type = EmbeddingModelTextType.get_type(self.provider, text_type)

        if self.provider == EmbeddingProvider.COHERE:
            return self._embed_cohere(text, model, embedding_type)
        elif self.provider == EmbeddingProvider.VOYAGE:
            return self._embed_voyage(text, model, embedding_type)
        elif self.provider == EmbeddingProvider.GOOGLE:
            return self._embed_vertex(text, model, embedding_type)
        else:
            raise ValueError(f"Unsupported provider: {self.provider}")

    def _embed_openai(self, text: str, model: str | None) -> list[float]:
        if model is None:
            model = DEFAULT_OPENAI_MODEL

        response = self.client.embeddings.create(input=text, model=model)
        return response.data[0].embedding

    def _embed_cohere(
        self, text: str, model: str | None, embedding_type: str
    ) -> list[float]:
        if model is None:
            model = DEFAULT_COHERE_MODEL

        response = self.client.embed(
            texts=[text],
            model=model,
            input_type=embedding_type,
        )
        return response.embeddings[0]

    def _embed_voyage(
        self, text: str, model: str | None, embedding_type: str
    ) -> list[float]:
        if model is None:
            model = DEFAULT_VOYAGE_MODEL

        response = self.client.embed(text, model=model, input_type=embedding_type)
        return response.embeddings[0]

    def _embed_vertex(
        self, text: str, model: str | None, embedding_type: str
    ) -> list[float]:
        if model is None:
            model = DEFAULT_VERTEX_MODEL

        embedding = self.client.get_embeddings(
            [
                TextEmbeddingInput(
                    text,
                    embedding_type,
                )
            ]
        )
        return embedding[0].values

    @staticmethod
    def create(
        api_key: str, provider: str, model: str | None = None
    ) -> "CloudEmbedding":
        logger.debug(f"Creating Embedding instance for provider: {provider}")
        return CloudEmbedding(api_key, provider, model)


def get_embedding_model(
    model_name: str,
    max_context_length: int,
) -> "SentenceTransformer":
    from sentence_transformers import SentenceTransformer  # type: ignore

    global _GLOBAL_MODELS_DICT  # A dictionary to store models

    if _GLOBAL_MODELS_DICT is None:
        _GLOBAL_MODELS_DICT = {}

    if model_name not in _GLOBAL_MODELS_DICT:
        logger.info(f"Loading {model_name}")
        model = SentenceTransformer(model_name)
        model.max_seq_length = max_context_length
        _GLOBAL_MODELS_DICT[model_name] = model
    elif max_context_length != _GLOBAL_MODELS_DICT[model_name].max_seq_length:
        _GLOBAL_MODELS_DICT[model_name].max_seq_length = max_context_length

    return _GLOBAL_MODELS_DICT[model_name]


def get_local_reranking_model_ensemble(
    model_names: list[str] = CROSS_ENCODER_MODEL_ENSEMBLE,
    max_context_length: int = CROSS_EMBED_CONTEXT_SIZE,
) -> list[CrossEncoder]:
    global _RERANK_MODELS
    if _RERANK_MODELS is None or max_context_length != _RERANK_MODELS[0].max_length:
        del _RERANK_MODELS
        gc.collect()

        _RERANK_MODELS = []
        for model_name in model_names:
            logger.info(f"Loading {model_name}")
            model = CrossEncoder(model_name)
            model.max_length = max_context_length
            _RERANK_MODELS.append(model)
    return _RERANK_MODELS


def warm_up_cross_encoders() -> None:
    logger.info(f"Warming up Cross-Encoders: {CROSS_ENCODER_MODEL_ENSEMBLE}")

    cross_encoders = get_local_reranking_model_ensemble()
    [
        cross_encoder.predict((MODEL_WARM_UP_STRING, MODEL_WARM_UP_STRING))
        for cross_encoder in cross_encoders
    ]


@simple_log_function_time()
def embed_text(
    texts: list[str],
    text_type: EmbedTextType,
    model_name: str | None,
    max_context_length: int,
    normalize_embeddings: bool,
    api_key: str | None,
    provider_type: str | None,
) -> list[list[float]]:
    if provider_type is not None:
        if api_key is None:
            raise RuntimeError("API key not provided for cloud model")

        cloud_model = CloudEmbedding(
            api_key=api_key, provider=provider_type, model=model_name
        )
        embeddings = cloud_model.encode(texts, model_name, text_type)

    elif model_name is not None:
        hosted_model = get_embedding_model(
            model_name=model_name, max_context_length=max_context_length
        )
        embeddings = hosted_model.encode(
            texts, normalize_embeddings=normalize_embeddings
        )

    if embeddings is None:
        raise RuntimeError("Embeddings were not created")

    if not isinstance(embeddings, list):
        embeddings = embeddings.tolist()
    return embeddings


@simple_log_function_time()
def calc_sim_scores(query: str, docs: list[str]) -> list[list[float]]:
    cross_encoders = get_local_reranking_model_ensemble()
    sim_scores = [
        encoder.predict([(query, doc) for doc in docs]).tolist()  # type: ignore
        for encoder in cross_encoders
    ]
    return sim_scores


@router.post("/bi-encoder-embed")
async def process_embed_request(
    embed_request: EmbedRequest,
) -> EmbedResponse:
    try:
        embeddings = embed_text(
            texts=embed_request.texts,
            model_name=embed_request.model_name,
            max_context_length=embed_request.max_context_length,
            normalize_embeddings=embed_request.normalize_embeddings,
            api_key=embed_request.api_key,
            provider_type=embed_request.provider_type,
            text_type=embed_request.text_type,
        )
        return EmbedResponse(embeddings=embeddings)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cross-encoder-scores")
async def process_rerank_request(embed_request: RerankRequest) -> RerankResponse:
    """Cross encoders can be purely black box from the app perspective"""
    if INDEXING_ONLY:
        raise RuntimeError("Indexing model server should not call intent endpoint")

    try:
        sim_scores = calc_sim_scores(
            query=embed_request.query, docs=embed_request.documents
        )
        return RerankResponse(scores=sim_scores)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
