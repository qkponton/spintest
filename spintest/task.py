"""Task representation."""

import asyncio

import jinja2
import json
import requests
import time

from urllib.parse import urljoin

from spintest import logger
from spintest.validator import input_validator, TASK_SCHEMA
from spintest.types import type_aware_encoder


class Task(object):
    """Task handler."""

    def __init__(self, url: str, task: dict, output: dict, verify: bool = True):
        """Initialization of `Task` class."""
        self.url = url
        self.task = task
        self.rollback = self.task.pop("rollback", None)
        self.output = output
        self.verify = verify
        self.response = None

    def _response(self, status: str, message: str) -> dict:
        """Return the response with logging."""
        result = {
            "name": self.task.get("name"),
            "status": status,
            "timestamp": time.asctime(),
            "url": self.url,
            "route": self.task.get("route", "/"),
            "message": message,
            "code": self._response_code(),
            "body": self._response_body(),
            "task": self.task,
            "ignore": self.task.get("ignore", False),
        }

        log_level = {"SUCCESS": logger.info, "FAILED": logger.error}
        log_level.get(status, logger.critical)(json.dumps(result, indent=4))

        result["output"] = self.output
        return result

    def _response_code(self):
        """Response code formatter."""
        try:
            return self.response.status_code
        except AttributeError:
            return None

    def _response_body(self):
        """Response body formatter."""
        try:
            return self.response.json()
        except json.JSONDecodeError:
            return self.response.text
        except AttributeError:
            return None

    def validate_code(self):
        """Validate the returned status code."""
        expected_code = self.task.get("expected", {}).get("code")
        response_code = self._response_code()
        if expected_code and expected_code != response_code:
            return self._response("FAILED", "Invalid HTTP status code.")

        if not (expected_code or 200 <= response_code < 300):
            return self._response("FAILED", "Invalid default HTTP status code (2XX).")

    def _expected_match(self):
        return self.task.get("expected", {}).get("expected_match", "strict")

    def _compare_body(self, body, expected):
        """Recursive comparison of body.
        if expected value is None, any body is accepted.
        if expected value is set, and expect_match value is not set,
            strict comparison is made.
        if expected value is set, and expect_match value is set to "partial",
            partial comparison is made.
        if expected and body keys are different, returns false.
        """
        if expected is None:
            return True

        if not isinstance(body, type(expected)):
            return False

        if isinstance(body, dict):
            if self._expected_match() == "strict" and body.keys() != expected.keys():
                return False

            if not set(expected).issubset(body):
                return False

            return all(
                [self._compare_body(body[ek], expected[ek]) for ek in expected.keys()]
            )

        elif isinstance(body, list):
            if self._expected_match() == "strict":
                if len(body) != len(expected):
                    return False
                for body_item in body:
                    for expected_item in expected:
                        if self._compare_body(body_item, expected_item):
                            break
                    else:
                        return False
            else:
                for expected_item in expected:
                    for body_item in body:
                        if self._compare_body(body_item, expected_item):
                            break
                    else:
                        return False
            return True

        else:
            if body == expected:
                return True
            return False

    def validate_body(self):
        """Validate the returned body."""
        expected_body = self.task.get("expected", {}).get("body")
        response_body = self._response_body()
        if expected_body and not self._compare_body(response_body, expected_body):
            return self._response(
                "FAILED",
                "The response body does not correspond with the expected body.",
            )

    async def run(self) -> dict:
        """Run the task on a specified URL."""

        # -- Input validation --

        validated_task = input_validator(self.task, TASK_SCHEMA)
        if not validated_task:
            return self._response(
                "FAILED", f"Task must follow this schema : {TASK_SCHEMA}."
            )

        self.task = validated_task

        self.task["method"] = self.task["method"].upper()
        if self.task["method"] not in [
            "GET",
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
            "HEAD",
            "CONNECT",
            "OPTIONS",
            "TRACE",
        ]:
            return self._response("FAILED", "Invalid HTTP method.")

        # Jinja2 logic substitution
        template = jinja2.Template(
            json.dumps(self.task, cls=type_aware_encoder(self.output))
        )
        self.task = json.loads(template.render(**self.output))

        self.task["headers"] = {
            **{"Accept": "application/json", "Content-Type": "application/json"},
            **self.task.get("headers", {}),
        }

        # -- Request --

        loop = asyncio.get_event_loop()

        for _ in range(self.task["retry"] + 1):
            try:
                if self.output.get("__token__"):
                    token = self.output["__token__"]
                    self.task["headers"]["Authorization"] = "Bearer " + (
                        token() if callable(token) else token
                    )
                self.response = await loop.run_in_executor(
                    None,
                    lambda: getattr(requests, self.task["method"].lower())(
                        urljoin(self.url, self.task["route"]),
                        json=self.task.get("body"),
                        headers=self.task["headers"],
                        verify=self.verify,
                    ),
                )
            except requests.exceptions.RequestException:
                failed_response = self._response("FAILED", "Request failed.")
                await asyncio.sleep(self.task["delay"])
                continue

            # -- Output validation --

            failed_response = self.validate_code()
            if failed_response is not None:
                await asyncio.sleep(self.task["delay"])
                continue

            failed_response = self.validate_body()
            if failed_response is not None:
                await asyncio.sleep(self.task["delay"])
                continue

            output_variable = self.task.get("output")
            if output_variable:
                self.output[output_variable] = self._response_body()

            return self._response("SUCCESS", "OK.")

        return failed_response
