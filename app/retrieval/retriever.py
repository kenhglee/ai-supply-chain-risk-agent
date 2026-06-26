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


def get_retriever(profiles_path: Path) -> FaissRetriever:
    provider = os.getenv("RETRIEVER_PROVIDER", "faiss").lower()
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()

    if provider == "faiss":
        return FaissRetriever(
            profiles_path=profiles_path,
            embedding_provider=embedding_provider,
            top_k=4,
        )

    raise ValueError(f"Unsupported RETRIEVER_PROVIDER: '{provider}'")
