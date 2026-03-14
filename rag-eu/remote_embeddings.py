import os
import logging
import httpx
import numpy as np
from typing import List, Union

logger = logging.getLogger(__name__)

class RemoteEmbeddings:
    def __init__(self, endpoint_url: str = None):
        if endpoint_url is None:
            endpoint_url = os.getenv("EMBEDDINGS_SERVICE_URL", "http://embeddings-eu:4006/embed")
        self.endpoint_url = endpoint_url
        
    def encode(self, sentences: Union[str, List[str]], **kwargs) -> np.ndarray:
        if isinstance(sentences, str):
            sentences = [sentences]
            
        try:
            # Internal HTTPS with SSL verification disabled (matches STT_SKIP_SSL_VERIFY pattern)
            with httpx.Client(timeout=10.0, follow_redirects=True, verify=False) as client:
                response = client.post(self.endpoint_url, json={"sentences": sentences})
                response.raise_for_status()
                data = response.json()
                embeddings_list = data.get("embeddings", [])
                return np.array(embeddings_list)
        except httpx.ConnectError as e:
            logger.warning(f"Embeddings service unreachable ({self.endpoint_url}): {e}")
            return np.zeros((len(sentences), 384))
        except Exception as e:
            logger.error(f"Failed to get remote embeddings: {e}")
            return np.zeros((len(sentences), 384))
