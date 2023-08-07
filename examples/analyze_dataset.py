from gpt_code_interpreter import CodeInterpreterSession, File


async def main():
    # context manager for start/stop of the session
    async with CodeInterpreterSession() as session:
        # define the user request
        user_request = "Analyze this dataset and plot something interesting about it."
        files = [
            File.from_path("examples/assets/iris.csv"),
        ]

        # generate the response
        response = await session.generate_response(
            user_request, files=files
        )

        # output the response (text + image)
        print("AI: ", response.content)
        for file in response.files:
            file.show_image()


if __name__ == "__main__":
    import asyncio

    # run the async function
    asyncio.run(main())
