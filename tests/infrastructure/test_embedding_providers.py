from unittest.mock import MagicMock

import numpy as np
import pytest

from app.application.ports.embeddings import IEmbeddingProvider
from app.infrastructure.embeddings.local_adapter import LocalEmbeddingAdapter


def test_local_embedding_adapter_implements_protocol():
    adapter = LocalEmbeddingAdapter(model=MagicMock())
    assert isinstance(adapter, IEmbeddingProvider)


@pytest.mark.asyncio
async def test_local_embedding_adapter_generate_embeddings():
    mock_model = MagicMock()
    mock_model.get_sentence_embedding_dimension.return_value = 384
    mock_model.encode.return_value = np.array([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])

    adapter = LocalEmbeddingAdapter(model_name="test-model", model=mock_model)

    result = await adapter.generate_embeddings(["hello", "world"])

    assert result.model_name == "test-model"
    assert result.dimensions == 384
    assert len(result.vectors) == 2
    assert result.vectors[0] == [0.1, 0.2, 0.3]
    assert result.vectors[1] == [0.4, 0.5, 0.6]
    mock_model.encode.assert_called_once_with(["hello", "world"], convert_to_numpy=True)
