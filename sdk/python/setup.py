from setuptools import setup, find_packages

setup(
    name="vouchstone-sdk",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.25.0",
        "pydantic>=2.0.0",
        "openai>=1.0.0",
        "anthropic>=0.18.0",
        "numpy>=1.24.0",
        "aiofiles>=23.0.0",
    ],
    extras_require={
        "vector": ["chromadb>=0.4.0", "qdrant-client>=1.7.0"],
        "graph": ["neo4j>=5.0.0"],
    },
    python_requires=">=3.10",
    author="Vouchstone",
    description="Python SDK for Vouchstone AI Agent Platform",
)
