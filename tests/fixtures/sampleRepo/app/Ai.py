"""OpenAI usage pinned to a model id that gets deprecated."""

from openai import OpenAI

llm = OpenAI()

DEFAULT_MODEL = "gpt-4-vision-preview"


def describe(image_url):
    completion = llm.chat.completions.create(
        model="gpt-4-vision-preview",
        messages=[{"role": "user", "content": image_url}],
    )
    return completion.choices[0].message.content


def describe_with_default(image_url):
    return llm.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[{"role": "user", "content": image_url}],
    )
