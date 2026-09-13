# Privacy (self-hosted Ask ALYF)

This notice is for **operators who deploy Ask ALYF from source** on their own infrastructure. It is not legal advice.

## What we do not collect

The published **source code** does not send usage data, analytics, or the contents of your chats to the maintainers of this project. We do not operate your instance; you do.

## Where data does go

Ask ALYF is designed to call **large language model (LLM) and related APIs that you configure** (credentials, base URLs, models, and any optional features such as speech). Any text, context, or documents you send through the app may be processed by those providers under **their** privacy policies and terms. You are responsible for choosing providers and settings that meet your requirements.

If you enable LangSmith tracing in **Ask ALYF Settings**, traces of agent runs go to LangSmith. Traces include prompts, tool calls, and answers. They go to the endpoint and project you configure. Tracing is off unless you turn it on.

## Your deployment

On your own servers, data may also be logged, cached, or backed up like any other Frappe app—depending on your hosting, monitoring, and security configuration. That is outside the scope of this repository.

If you need a policy for **end users** of your site (e.g. employees), prepare one that covers your organisation, your LLM vendors, and applicable law.
