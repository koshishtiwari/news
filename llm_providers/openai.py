import logging
import json
import asyncio
import aiohttp
import time
from typing import Optional, Dict, Any

# Import base and global collector
from .base import LLMProvider
from monitor.metrics import metrics_collector

logger = logging.getLogger(__name__)

class OpenAILLM(LLMProvider):
    """Integration with OpenAI API with error handling and retry logic."""

    def __init__(self, model: str, api_key: str, request_timeout: int = 30,
                 max_retries: int = 3, retry_delay: float = 2.0):
        self.model = model
        self.api_key = api_key
        self.api_endpoint = "https://api.openai.com/v1/chat/completions"
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._session: Optional[aiohttp.ClientSession] = None
        logger.info(f"Initialized OpenAILLM: model={self.model}, timeout={self.request_timeout}s")

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                connector=aiohttp.TCPConnector(limit=5)
            )
        return self._session

    async def close(self):
        """Close the session when done using this provider."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.debug("Closed OpenAI session")

    def _prepare_request_data(self, prompt: str, system_prompt: Optional[str] = None) -> Dict[str, Any]:
        """Prepare the request data for OpenAI API."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1024
        }
        return data

    async def generate(self, prompt: str, system_prompt: str = None) -> str:
        """
        Generates text based on the provided prompt with retry logic.
        
        Args:
            prompt: The main user prompt
            system_prompt: Optional system-level instruction
            
        Returns:
            Generated text or error message
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        data = self._prepare_request_data(prompt, system_prompt)
        
        logger.debug(f"OpenAI Request: model={self.model}, system={bool(system_prompt)}, prompt_len={len(prompt)}")

        response_text = ""
        is_error = True  # Assume error initially
        start_time = time.monotonic()
        latency = 0.0
        
        # Implement retry logic for transient errors
        retries = 0
        last_error = None

        while retries <= self.max_retries:
            if retries > 0:
                wait_time = self.retry_delay * retries
                logger.info(f"Retrying OpenAI request (attempt {retries}/{self.max_retries}) after {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
            
            try:
                session = await self._get_session()
                async with session.post(
                    self.api_endpoint, 
                    headers=headers, 
                    json=data,
                    raise_for_status=False
                ) as response:
                    if response.status == 200:
                        try:
                            result = await response.json()
                            # Extract text from OpenAI's response format
                            response_text = result["choices"][0]["message"]["content"].strip()
                            if not response_text:
                                logger.warning("OpenAI returned empty response with status 200")
                                last_error = ValueError("Empty response from OpenAI")
                                retries += 1
                                continue
                                
                            is_error = False  # Success!
                            logger.debug(f"OpenAI Response received (len={len(response_text)})")
                            break  # Exit retry loop on success
                        except (json.JSONDecodeError, KeyError) as e:
                            last_error = e
                            logger.error(f"Failed parsing OpenAI success response: {e}", exc_info=True)
                            # Try again if retries remain
                    elif response.status in (408, 429) or response.status >= 500:
                        # Rate limits, server errors or timeouts are candidates for retry
                        error_body = await response.text()
                        last_error = Exception(f"OpenAI API Error ({response.status}): {error_body[:100]}...")
                        logger.warning(f"Retryable OpenAI error: {response.status}")
                        retries += 1
                    else:
                        # Client errors (4xx except 408/429) are not retried
                        error_body = await response.text()
                        last_error = Exception(f"OpenAI API Error ({response.status}): {error_body[:100]}...")
                        logger.error(f"Non-retryable OpenAI API Error ({response.status}): {error_body[:500]}...")
                        break

            except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
                last_error = e
                logger.error(f"Connection or timeout error with OpenAI: {e}")
                retries += 1
            except Exception as e:
                last_error = e
                logger.error(f"Unexpected error calling OpenAI API: {e}", exc_info=True)
                retries += 1
        
            finally:
                latency = time.monotonic() - start_time
                metrics_collector.record_llm_call(
                    provider="openai",
                    model=self.model,
                    latency=latency,
                    is_error=is_error
                )
                logger.debug(f"OpenAI call complete: latency={latency:.3f}s, error={is_error}, retries={retries}")

        # If we've exhausted retries and still have an error
        if is_error and last_error:
            logger.error(f"All {retries} retries failed for OpenAI request. Last error: {last_error}")
            return f"Error: Unable to generate a response from the language model. Please try again later."

        return response_text
