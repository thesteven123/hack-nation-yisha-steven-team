# AI Lab - Human-guided scientific research

AI Lab connects literature evidence, candidate research directions, human decisions, bounded analyses, and review in an inspectable workspace.

**Team:** Yisha Tang and Steven Milligan-Villegas, University of California, Berkeley. We developed the idea together and separately experimented with different agent platforms.

## Try the browser companion

**Public working demo:** https://ai-lab-research-yisha-steven.xlyzx.chatgpt.site

The companion provides two functioning local tools: exact quotation checks against supplied sources and analysis of paired numerical data. It preserves input hashes and exports JSON records. The examples are synthetic. Exact occurrence does not establish scientific truth; leave-one-out sensitivity does not establish replication.

The browser companion does not run the desktop Literature Agent, Idea Agent, or native Codex model jobs. Those workflows require the desktop client, an independent service, and provider setup.

To run this companion locally, serve the repository with `python -m http.server 8000` and open `http://localhost:8000/browser-demo.html`. It makes no model calls and sends no input data to a backend. HTTPS or localhost enables input hashing.

## Desktop research workflow

The prototype extends the open-source AgentsDock platform:

1. A Literature Agent retrieves sources and records evidence quotations and reading coverage.
2. An Idea Agent compares research directions; a separate review context can request evidence.
3. The researcher chooses, combines, revises, or defers a direction.
4. A research workspace preserves chosen inputs, bounded analysis results, limitations, and separately recorded Planner/Analyst/Reviewer model jobs.

Implemented analysis families are quotation checks in supplied sources and paired numerical analysis. Local workflow records do not establish scientific effectiveness.

## Source and setup

- `electron/`: Electron, React, and TypeScript client; dependencies and lockfile are inside this package.
- `server/`: Python service and SQLite research records; Python environment and server installation instructions are self-contained here.
- `browser-demo.html`: the limited standalone companion's full source.
- Synthetic paired values and quotation examples are embedded in `browser-demo.html`.
- `docs/AI_LAB_QUICKSTART.md` and `docs/AI_LAB_ACCEPTANCE.md`: development startup notes and verified acceptance scope.
- Original platform documentation and distribution links are preserved below.

Desktop source development uses Node.js 24 and the pinned pnpm 11.9.0. In `electron/`, run `pnpm install --frozen-lockfile`, then `pnpm dev`. A separate running AgentsServer is required; follow `server/README.md` for its installation and authenticate native Codex on the server. Windows uses a separate WSL service; `Start-AILab.cmd` is the development entry point described in the quickstart, not a portable standalone installer.

Use **Settings > General > Language > English** for English interface labels. Historical user inputs and model outputs retain their original language.

## What is limited today

Literature coverage is bounded. Document extraction is text-based and does not cover scanned pages, figures, or equation layout. The analysis adapters are limited; arbitrary scientific experiments and a portable desktop release remain future work. The complete v0.5 milestone has not been accepted.

## Attribution and licenses

The desktop and server foundation is [AgentsDock](https://github.com/ZhengyiLuo/AgentsDock), licensed under Apache-2.0 except for separately licensed components. Its `LICENSE`, `NOTICE`, and third-party notices remain intact. Our project contribution is the research workflow and bounded browser companion; we do not claim authorship of the entire underlying platform.

This English submission branch was prepared from the team's public AI Lab development snapshot `97c80cfd6c6b175a8843027ca3f674404b199313`; the original main and development branches are preserved.


---

## Original platform documentation

**AI Lab 独立开发分支**

这是队伍的独立 AI Lab 开发分支，已提供有限工作流；完整 v0.5 尚未验收。
[快速上手](docs/AI_LAB_QUICKSTART.md) · [验收范围与缺口](docs/AI_LAB_ACCEPTANCE.md) · [Windows 启动入口](Start-AILab.cmd)

<h1 align="center">AgentsDock: an IDE designed for agentic AI research</h1>

<div align="center">
  <a href="https://agentsdock.net">
    <img
      src="https://img.shields.io/badge/website-agentsdock.net-0EA5E9"
      alt="AgentsDock website"
    />
  </a>
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/tag/v1.0.9">
    <img
      src="https://img.shields.io/badge/desktop-v1.0.9-EA7233"
      alt="Desktop 1.0.9 stable"
    />
  </a>
  <a href="https://discord.gg/ZGDrhEWqPt">
    <img
      src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&amp;logoColor=white"
      alt="Join the AgentsDock Discord"
    />
  </a>
  <img
    src="https://img.shields.io/badge/backend-self--hosted-2563EB"
    alt="Self-hosted backend"
  />
  <a href="CONTRIBUTING.md">
    <img
      src="https://img.shields.io/badge/contributions-welcome-brightgreen.svg"
      alt="Contributions welcome"
    />
  </a>
</div>

<p align="center">
  <a href="https://agentsdock.net">
    <img
      src="docs/assets/agentsdock-overview.png"
      alt="AgentsDock desktop and mobile apps showing agent chats and file previews"
      width="760"
    />
  </a>
</p>

<div align="center">
  AgentsDock brings <strong>Claude Code</strong>, <strong>Codex</strong>,
  <strong>Cursor</strong>, and <strong>OpenCode</strong> into one desktop and
  mobile workspace. Use your agents for coding, research, and long-running work
  without living in a terminal. Easily review files and rich media.
  <br />
  <br />
  <strong>Desktop stable 1.0.9:</strong>
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/download/v1.0.9/AgentsDock-1.0.9-mac-universal.dmg">macOS</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/download/v1.0.9/AgentsDock-1.0.9-linux-x86_64.AppImage">Linux x86_64</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/download/v1.0.9/AgentsDock-1.0.9-linux-arm64.AppImage">Linux ARM64</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/download/v1.0.9/AgentsDock-1.0.9-win-x64.exe">Windows (unsigned installer)</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases">Release page</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock/releases/tag/v1.0.9">1.0.9 release notes</a>
  <br />
  <strong>Mobile:</strong>
  <a href="https://apps.apple.com/us/app/agentsdock/id6769275751">iPhone &amp; iPad</a>
  ·
  <a href="https://github.com/ZhengyiLuo/AgentsDock-Releases/releases/download/android-v0.1.1-beta.8/AgentsDock-0.1.1-android-arm64-beta.8.apk">Android</a>
  <br />
  <br />
  <strong>
    AgentsDock is the client; AgentsServer is the self-hosted backend.
  </strong>
</div>

## What you can do

- **Work with your agents:** start and resume chats, follow live activity, and
  queue the next task.
- **Ask a side question:** Codex Side chat has independent model and reasoning
  controls, live activity, and Stop/Clear actions alongside your main chat.
- **Review the results:** view images and videos inline, browse files, inspect
  code changes, and download artifacts.
- **Keep long-running work organized:** group chats, search history, create
  digests, and schedule recurring jobs.
- **Open a real terminal:** use a persistent tmux terminal attached to each
  chat's workspace.
- **Move between devices and servers:** connect multiple clients to the same
  server, or manage several servers from one app.

## How it works

**AgentsDock is the client. AgentsServer is the self-hosted backend.** Install
the server on the machine that has your projects and agent CLIs. That can be
the same computer as the desktop app or a remote machine you control. The
desktop and mobile apps connect to it to send tasks and display results.

The agent CLIs must be installed and authenticated **on the server**, not on
your phone. Self-hosting gives you control of the server and stored history;
it does not make the models local. Your selected provider still processes
model requests, and clients may cache content on your devices. Available
providers and features depend on the client and server version.

## Get started

1. **Install the app.** Get a desktop build from
   [public GitHub Releases](https://github.com/ZhengyiLuo/AgentsDock/releases)
   ([current stable: 1.0.9](https://github.com/ZhengyiLuo/AgentsDock/releases/tag/v1.0.9)),
   or find the current iPhone/iPad distribution link on
   [the website](https://agentsdock.net/#downloads).
2. **Set up AgentsServer.** Direct macOS and Linux builds provide
   **Set up AgentsServer**. For a fresh manual installation on Linux or Apple
   silicon macOS, install Node.js/npm and
   [`uv`](https://docs.astral.sh/uv/getting-started/installation/), then run:

   ```sh
   npx @agentsdock/server install
   ```

   This installs the latest stable server, normally on port **7850**. Run it as
   the user who will own the server, without `sudo`. Existing installations
   use the app's managed updates; this command refuses existing server state.
   See [server setup and prerequisites](server/README.md#get-started).
3. **Connect and start a chat.** Add the server connection in AgentsDock,
   using the URL and access token printed by the installer. Install the agent
   CLI you want on the server, then sign in or configure a supported API
   connection in **Settings → My Agents** on desktop. Choose an agent and a
   working directory on the server, and send a task.

**Already using AgentsDock?** Update the desktop app from Settings. After
relaunch, 1.0.9 coordinates each supported server's matching signed update;
busy servers wait until idle, and offline servers resume after reconnecting.
The signed 1.0.9 server bridge remains available for supported older
installations. See the [release notes](https://github.com/ZhengyiLuo/AgentsDock/releases/tag/v1.0.9)
for upgrade validation limits.

For remote access, use a private network such as Tailscale rather than exposing
the server directly to the internet. The server needs `tmux` for persistent
terminals; see its documentation for the full prerequisites. Connection help
is in the [setup guide](https://agentsdock.net/setup.html).

## Develop from source

Use Git, **Node.js 24**, and **pnpm 11.9.0** (the version pinned by the project).
Desktop and mobile are separate packages; install dependencies in the package
you are working on. You also need a running AgentsServer to use the app.

```bash
git clone https://github.com/ZhengyiLuo/AgentsDock.git
cd AgentsDock
```

### Desktop

The current desktop app is built with Electron, React, and TypeScript. From
the repository root:

```bash
cd electron
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
pnpm dev
```

`pnpm build` compiles the app without packaging or publishing it. For desktop
architecture and platform-specific packaging, see
[electron/README.md](electron/README.md).

### Mobile: iPhone, iPad, and Android source

The current iPhone/iPad app lives in **`mobile-react/`**, using React Native
and Expo. Android source and native modules live in that same package. From
the repository root:

```bash
cd mobile-react
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test:history
pnpm test:server-setup
```

For local iOS development on macOS with Xcode, continue in `mobile-react/`:

```bash
pnpm exec expo prebuild --platform ios
pnpm exec expo run:ios
```

Use a simulator or your own signing configuration. Read the
[mobile development guide](mobile-react/README.md) before installing on a
device: using the existing bundle identifier can replace your installed app.
Android development requires the Android toolchain.

## Where things live

| Directory | Purpose |
| --- | --- |
| [`electron/`](electron/) | Current desktop client for macOS, Linux, and Windows |
| [`mobile-react/`](mobile-react/) | Current React Native mobile client, including native modules in `modules/` |
| [`website/`](website/) | Product website and user guides |
| [`team-hub/`](team-hub/) | Team Hub service code and tests |
| [`docs/`](docs/) | Architecture, development notes, and release-channel documentation |
| [`server/`](server/) | Maintained AgentsServer runtime, installer, and server-only tests |
| `Sources/`, `Apps/`, `ZenithDock.xcodeproj` | Legacy Swift clients, not the current Electron or React Native apps |

The maintained backend lives in `server/`. Its Python dependencies and installer
are self-contained; server users do not need to build either client. See
[the source migration notes](docs/SERVER_SOURCE_MIGRATION.md) before exporting
changes or preparing a coordinated release.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks and contribution guidelines.
Keep changes scoped and add regression tests where appropriate. Use synthetic
test data; do not commit credentials, private infrastructure details, chat
transcripts, or screenshots containing user data.

The repository's CI verifies source. It does not publish desktop releases,
upload mobile builds, or deploy servers. Official binaries and update feeds
are managed separately; see [release channels](docs/DIRECT_RELEASES.md).

## License

AgentsDock's original code is licensed under the [Apache License 2.0](LICENSE),
except where a component explicitly states another license. See [NOTICE](NOTICE)
for attribution and separately licensed components. Existing third-party
copyrights, licenses, and notices remain in effect.
