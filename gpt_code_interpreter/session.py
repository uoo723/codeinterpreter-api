import base64
import re
import traceback
import uuid
from dataclasses import dataclass
from io import BytesIO
from os import getenv
from typing import Callable, Literal, Optional

from gpt_code_interpreter.agents import OpenAIFunctionsAgent
from gpt_code_interpreter.chains import get_file_modifications, remove_download_link
from gpt_code_interpreter.config import settings
from gpt_code_interpreter.prompts import code_interpreter_system_message
from gpt_code_interpreter.schema import (
    CodeInput,
    CodeInterpreterResponse,
    File,
    UserRequest,
)
from gpt_code_interpreter.utils import (
    CodeAgentOutputParser,
    CodeCallbackHandler,
    CodeChatAgentOutputParser,
)
from codeboxapi import CodeBox  # type: ignore
from codeboxapi.schema import CodeBoxOutput  # type: ignore
from langchain.agents import (
    AgentExecutor,
    BaseSingleActionAgent,
    ConversationalAgent,
    ConversationalChatAgent,
)
from langchain.chat_models import ChatAnthropic, ChatOpenAI
from langchain.chat_models.base import BaseChatModel
from langchain.memory import ConversationBufferMemory
from langchain.prompts.chat import MessagesPlaceholder
from langchain.schema.language_model import BaseLanguageModel
from langchain.tools import BaseTool, StructuredTool


@dataclass
class Output:
    content: str | File
    type: Literal["code", "str", "image", "file", "error"]


OnOutput = Callable[[Output], None]


class CodeInterpreterSession:
    def __init__(
        self,
        llm: Optional[BaseLanguageModel] = None,
        additional_tools: list[BaseTool] = [],
        **kwargs,
    ) -> None:
        self.codebox = CodeBox()
        self.verbose = kwargs.get("verbose", settings.VERBOSE)
        self.tools: list[BaseTool] = self._tools(additional_tools)
        self.llm: BaseLanguageModel = llm or self._choose_llm(**kwargs)
        self.agent_executor: AgentExecutor = self._agent_executor()
        self.input_files: list[File] = []
        self.output_files: list[File] = []
        self.on_output: OnOutput = lambda x: None

    def start(self) -> None:
        self.codebox.start()

    async def astart(self) -> None:
        if type(self.codebox) != CodeBox:
            # check if jupyter-kernel-gateway is installed
            import pkg_resources  # type: ignore

            try:
                pkg_resources.get_distribution("jupyter-kernel-gateway")
            except pkg_resources.DistributionNotFound:
                print(
                    "Make sure 'jupyter-kernel-gateway' is installed when using without a CODEBOX_API_KEY.\n"
                    "You can install it with 'pip install jupyter-kernel-gateway'."
                )
                exit(1)
        await self.codebox.astart()

    def _tools(self, additional_tools: list[BaseTool]) -> list[BaseTool]:
        return additional_tools + [
            StructuredTool(
                name="python",
                description=
                # TODO: variables as context to the agent
                # TODO: current files as context to the agent
                "Input a string of code to a python interpreter (jupyter kernel). "
                "Write the entire code in a single string. This string can "
                "be really long, so you can use the `;` character to split lines. "
                "Variables are preserved between runs. ",
                func=self._run_handler,
                coroutine=self._arun_handler,
                args_schema=CodeInput,
            ),
        ]

    def _choose_llm(
        self, model: str = "gpt-4", openai_api_key: Optional[str] = None, **kwargs
    ) -> BaseChatModel:
        if "gpt" in model:
            openai_api_key = (
                openai_api_key
                or settings.OPENAI_API_KEY
                or getenv("OPENAI_API_KEY", None)
            )
            if openai_api_key is None:
                raise ValueError(
                    "OpenAI API key missing. Set OPENAI_API_KEY env variable or pass `openai_api_key` to session."
                )
            return ChatOpenAI(
                temperature=0.03,
                model=model,
                openai_api_key=openai_api_key,
                max_retries=3,
                request_timeout=60 * 3,
            )  # type: ignore
        elif "claude" in model:
            return ChatAnthropic(model=model)
        else:
            raise ValueError(f"Unknown model: {model} (expected gpt or claude model)")

    def _choose_agent(self) -> BaseSingleActionAgent:
        return (
            OpenAIFunctionsAgent.from_llm_and_tools(
                llm=self.llm,
                tools=self.tools,
                system_message=code_interpreter_system_message,
                extra_prompt_messages=[
                    MessagesPlaceholder(variable_name="chat_history")
                ],
            )
            if isinstance(self.llm, ChatOpenAI)
            else ConversationalChatAgent.from_llm_and_tools(
                llm=self.llm,
                tools=self.tools,
                system_message=code_interpreter_system_message.content,
                output_parser=CodeChatAgentOutputParser(),
            )
            if isinstance(self.llm, BaseChatModel)
            else ConversationalAgent.from_llm_and_tools(
                llm=self.llm,
                tools=self.tools,
                prefix=code_interpreter_system_message.content,
                output_parser=CodeAgentOutputParser(),
            )
        )

    def _agent_executor(self) -> AgentExecutor:
        return AgentExecutor.from_agent_and_tools(
            agent=self._choose_agent(),
            callbacks=[CodeCallbackHandler(self)],
            max_iterations=9,
            tools=self.tools,
            verbose=self.verbose,
            memory=ConversationBufferMemory(
                memory_key="chat_history", return_messages=True
            ),
        )

    async def show_code(self, code: str) -> None:
        """Callback function to show code to the user."""
        if self.verbose:
            print(code)

    def _run_handler(self, code: str):
        raise NotImplementedError("Use arun_handler for now.")

    async def _arun_handler(self, code: str):
        """Run code in container and send the output to the user"""
        print("Running code in container...", code)

        self.on_output(Output(content=code, type="code"))

        output: CodeBoxOutput = await self.codebox.arun(code)

        if not isinstance(output.content, str):
            raise TypeError("Expected output.content to be a string.")

        if output.type == "image/png":
            filename = f"image-{uuid.uuid4()}.png"
            file_buffer = BytesIO(base64.b64decode(output.content))
            file_buffer.name = filename

            file = File(name=filename, content=file_buffer.read())

            self.on_output(Output(content=file, type="image"))
            self.output_files.append(file)

            return f"Image {filename} got send to the user."

        elif output.type == "error":
            if "ModuleNotFoundError" in output.content:
                if package := re.search(
                    r"ModuleNotFoundError: No module named '(.*)'", output.content
                ):
                    await self.codebox.ainstall(package.group(1))
                    return f"{package.group(1)} was missing but got installed now. Please try again."
            else:
                # TODO: preanalyze error to optimize next code generation
                pass

            self.on_output(Output(conatent=output.content, type="error"))

            if self.verbose:
                print("Error:", output.content)

        elif modifications := await get_file_modifications(code, self.llm):
            for filename in modifications:
                if filename in [file.name for file in self.input_files]:
                    continue
                fileb = await self.codebox.adownload(filename)
                if not fileb.content:
                    continue
                file_buffer = BytesIO(fileb.content)
                file_buffer.name = filename

                file = File(name=filename, content=file_buffer.read())
                self.output_files.append(file)

                self.on_output(Output(content=file, type="file"))

        else:
            self.on_output(Output(content=output.content, type="str"))

        return output.content

    async def _input_handler(self, request: UserRequest):
        if not request.files:
            return
        if not request.content:
            request.content = (
                "I uploaded, just text me back and confirm that you got the file(s)."
            )
        request.content += "\n**The user uploaded the following files: **\n"
        for file in request.files:
            self.input_files.append(file)
            request.content += f"[Attachment: {file.name}]\n"
            await self.codebox.aupload(file.name, file.content)
        request.content += "**File(s) are now available in the cwd. **\n"

    async def _output_handler(self, final_response: str) -> CodeInterpreterResponse:
        """Embed images in the response"""
        for file in self.output_files:
            if str(file.name) in final_response:
                # rm ![Any](file.name) from the response
                final_response = re.sub(rf"\n\n!\[.*\]\(.*\)", "", final_response)

        if self.output_files and re.search(rf"\n\[.*\]\(.*\)", final_response):
            try:
                final_response = await remove_download_link(final_response, self.llm)
            except Exception as e:
                if self.verbose:
                    print("Error while removing download links:", e)

        return CodeInterpreterResponse(content=final_response, files=self.output_files)

    async def generate_response(
        self,
        user_msg: str,
        files: list[File] = [],
        detailed_error: bool = False,
        on_output: OnOutput = lambda x: None,
    ) -> CodeInterpreterResponse:
        """Generate a Code Interpreter response based on the user's input."""
        self.on_output = on_output

        user_request = UserRequest(content=user_msg, files=files)
        try:
            await self._input_handler(user_request)
            response = await self.agent_executor.arun(input=user_request.content)
            return await self._output_handler(response)
        except Exception as e:
            if self.verbose:
                traceback.print_exc()
            if detailed_error:
                return CodeInterpreterResponse(
                    content=f"Error in CodeInterpreterSession: {e.__class__.__name__}  - {e}"
                )
            else:
                return CodeInterpreterResponse(
                    content="Sorry, something went while generating your response."
                    "Please try again or restart the session."
                )

    async def is_running(self) -> bool:
        return await self.codebox.astatus() == "running"

    async def astop(self) -> None:
        await self.codebox.astop()

    async def __aenter__(self) -> "CodeInterpreterSession":
        await self.astart()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.astop()
