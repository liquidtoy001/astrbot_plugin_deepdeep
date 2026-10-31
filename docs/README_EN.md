<div align="center">

<img src="../logo.png" width="128" alt="DeepDeep" />

# DeepDeep

[简体中文](../README.md) | **English**

**A search manager for AstrBot: it helps your bot pick the right search tool, switches when one fails, and keeps spending under control.**

[![CI](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml/badge.svg)](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.26.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

## What it does

- **Picks the right tool**: tells the model which search tool to try first for each kind of question, such as DeepSeek for news, Tavily for programming and a video search for videos.
- **Switches on failure**: when a service runs out of quota or can't be reached, the model is steered to another tool and the conversation continues.
- **Controls spending**: estimates the cost of all paid search services and stops the paid tools at your monthly cap. Free tools keep working.
- **Shows status at a glance**: red, yellow and green lights show whether search works, what's missing and where to fill it in.
- **Keeps records**: every tool call is logged, so you can see what happened when something goes wrong.

If you already use other search tools, that's fine. DeepDeep doesn't replace them; it helps them work together.

## Get started in three steps

**1. Install**: search for "DeepDeep" in AstrBot's plugin market and install it. It needs AstrBot 4.26.0 or later.

**2. Set up a search service**: open the plugin dashboard. If no search is available, the Overview page walks you through choosing a service, entering a key and testing it. If you're not sure which to choose:

| Your situation | Recommended | Cost |
|---|---|---|
| You already use the official DeepSeek API in AstrBot | **DeepSeek**, which reuses that key automatically | About ¥5 per 1,000 searches |
| You want to try it for free | **Tavily** | 1,000 free searches a month |
| You have your own server | **SearXNG**, self-hosted, with its plugin installed | Free |

**3. Try it**: ask your bot something that needs the web, such as "what's in the tech news today?". Then go back to the dashboard. If the Overview light is green and the Call log has an entry, it worked.

Prices and free quotas can change, so check each service's website.

## What's in the dashboard

| Page | What it's for |
|---|---|
| Overview | Whether search works, this month's spending, today's usage |
| Usage | Spending share and calls per service |
| Call log | View, filter and export every tool call |
| Settings | Monthly spending cap, preference (recommended, save money, Chinese first), API keys |
| Tools & diagnostics | Generate diagnostics, check the network, see which tools each kind of question uses |

For everyday use you only need Overview and Settings. Settings save automatically, and the harder options are under Advanced, where you can leave them alone.

Screenshots show the Chinese interface; the dashboard can also switch to English.

<img src="https://raw.githubusercontent.com/liquidtoy001/astrbot_plugin_deepdeep/main/docs/images/overview.png" alt="Overview page: search status, this month's spending, today's usage and main search tools" />

<details>
<summary>Usage page screenshot</summary>

<img src="https://raw.githubusercontent.com/liquidtoy001/astrbot_plugin_deepdeep/main/docs/images/usage.png" alt="Usage page: searches over 30 days, spending share per service and usage per tool" />

</details>

<details>
<summary>Tools &amp; diagnostics page screenshot</summary>

<img src="https://raw.githubusercontent.com/liquidtoy001/astrbot_plugin_deepdeep/main/docs/images/tools.png" alt="Tools and diagnostics page: diagnostics, network connectivity and routing preview" />

</details>

## FAQ

**Do I need a DeepSeek chat model?**

No. Search and chat are separate, so any chat model works. You can also skip DeepSeek search and use only other tools.

**How much does it cost?**

The plugin is free. Costs come from the search services you choose. The default monthly cap is ¥30, and you can change it. The dashboard's spending is an estimate from public prices; each service's bill is what counts.

**Does it conflict with AstrBot's built-in web search?**

No, they work together. AstrBot's web search uses one provider at a time, and DeepDeep lets you add services such as Tavily on top. If AstrBot already uses Tavily, DeepDeep doesn't offer it twice.

**What should I do about a yellow or red light?**

Open it and read the hint. Usually a tool has no key, or its recent results were poor. Add the missing setting, stop the tool or ignore the warning.

**Does it affect prompt caching?**

Not normally. The cache is invalidated once when the tool list changes, for example when you add a key or reach the spending cap. In long conversations, that one time costs a bit more in model fees.

**Does it upload my data?**

Call logs stay on your own server. When searching, the search terms and the first 128 characters of the user's message go to DeepSeek (you can turn off sending the message). Other services also receive the search terms.

## Support

- **Chat platforms**: OneBot (aiocqhttp), webchat and Telegram are declared. The plugin has no platform-specific code, so others usually work too, but they haven't been checked one by one.
- **Experimental services**: Perplexity, Serper, Jina and TinyFish work once you add a key, but they haven't been verified against their real APIs. The dashboard marks them 未实测 (untested).

## More

- [Full guide](GUIDE_EN.md): routing modes, division-of-labor rules, evaluation data, privacy details
- [Changelog (Chinese)](../CHANGELOG.md)
- [Report a problem](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/issues/new/choose): please attach the diagnostics from the Tools & diagnostics page
- [Contributing](../.github/CONTRIBUTING.md)

If you find it useful, a ⭐ Star is welcome.

[MIT License](../LICENSE)
