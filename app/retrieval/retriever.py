import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RetrieverResult:
    context: str


class FaissRetriever:
    retriever_id = "faiss_supplier_profiles"
    retriever_version = "v1"

    def __init__(
        self,
        profiles_path: Path,
        embedding_provider: str = "openai",
        top_k: int = 4,
    ) -> None:
        self.embedding_provider = embedding_provider
        self.top_k = top_k
        self._vectorstore = self._build_vectorstore(profiles_path, embedding_provider)

    def _build_vectorstore(self, path: Path, embedding_provider: str):
        with open(path, "r", encoding="utf-8") as f:
            profiles = json.load(f)

        from langchain_core.documents import Document
        docs = [
            Document(page_content=item["profile"], metadata={"supplier": item["supplier"]})
            for item in profiles
        ]

        if embedding_provider == "openai":
            from langchain_openai import OpenAIEmbeddings
            embeddings = OpenAIEmbeddings()
        else:
            raise ValueError(f"Unsupported EMBEDDING_PROVIDER: '{embedding_provider}'")

        from langchain_community.vectorstores import FAISS
        return FAISS.from_documents(docs, embeddings)

    def retrieve(self, query: str, candidate_suppliers: list[str]) -> RetrieverResult:
        docs = self._vectorstore.similarity_search(query, k=self.top_k)

        if candidate_suppliers:
            filtered = [
                d for d in docs
                if d.metadata.get("supplier") in candidate_suppliers
            ]
            docs = filtered[:2] if filtered else docs[:2]
        else:
            docs = docs[:2]

        context = "\n\n".join(doc.page_content for doc in docs) if docs else "No context found"
        return RetrieverResult(context=context)


class BedrockKBRetriever:
    retriever_id = "bedrock_kb_supplier_profiles"
    retriever_version = "v1"
    embedding_provider = "bedrock_managed"

    def __init__(self, kb_id: str, region: str, top_k: int = 4) -> None:
        import boto3
        self.top_k = top_k
        self._kb_id = kb_id
        self._client = boto3.client("bedrock-agent-runtime", region_name=region)

    def retrieve(self, query: str, candidate_suppliers: list[str]) -> RetrieverResult:
        vector_config: dict = {"numberOfResults": self.top_k}
        if candidate_suppliers:
            vector_config["filter"] = {
                "in": {"key": "supplier", "value": candidate_suppliers}
            }
        response = self._client.retrieve(
            knowledgeBaseId=self._kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vector_config},
        )
        results = response.get("retrievalResults", [])[:2]
        context = "\n\n".join(r["content"]["text"] for r in results) or "No context found"
        return RetrieverResult(context=context)


def get_retriever(profiles_path: Path) -> FaissRetriever | BedrockKBRetriever:
    provider = os.getenv("RETRIEVER_PROVIDER", "faiss").lower()
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()

    if provider == "faiss":
        return FaissRetriever(
            profiles_path=profiles_path,
            embedding_provider=embedding_provider,
            top_k=4,
        )

    if provider == "bedrock_kb":
        kb_id = os.getenv("BEDROCK_KB_ID")
        if not kb_id:
            raise ValueError("BEDROCK_KB_ID must be set when RETRIEVER_PROVIDER=bedrock_kb")
        return BedrockKBRetriever(
            kb_id=kb_id,
            region=os.getenv("AWS_DEFAULT_REGION", "us-west-2"),
            top_k=int(os.getenv("BEDROCK_KB_TOP_K", "4")),
        )

    raise ValueError(f"Unsupported RETRIEVER_PROVIDER: '{provider}'")
