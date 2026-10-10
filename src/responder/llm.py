import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# langfuse sdks read LANGFUSE_HOST, the langfuse ui hands out LANGFUSE_BASE_URL
if os.getenv("LANGFUSE_BASE_URL") and not os.getenv("LANGFUSE_HOST"):
    os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]

providers = {
    # any openai-compatible endpoint works: groq, gemini, or vllm on colab
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-3.8-flash"),
    "vllm": (os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"), "VLLM_API_KEY", "openai/gpt-oss-120b"),
}


def make_llm(provider=None, model=None, temperature=0):
    provider = provider or os.getenv("LLM_PROVIDER", "groq")
    base_url, key_var, default_model = providers[provider]
    if provider == "gemini":
        # gemini's thinking models need their thought signatures sent back with every tool call,
        # which the openai-compatible endpoint drops, so use google's own client here
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model or os.getenv("LLM_MODEL", default_model),
                                      api_key=os.getenv(key_var), temperature=temperature, max_retries=10)
    return ChatOpenAI(
        base_url=base_url,
        api_key=os.getenv(key_var, "none"),
        model=model or os.getenv("LLM_MODEL", default_model),
        temperature=temperature,
        max_retries=10,  # rate limits come back as 429 with a retry-after, the client waits and retries
        timeout=120,
    )


def tracing_callbacks():
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]
