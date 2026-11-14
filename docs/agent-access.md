# Agent access

Shijhon has an MCP server that AI agents can connect to. A connected agent can make and
change playlists from a request such as "make a calm evening playlist from the library" or
"refresh the running mix". With permissions of their own, it can also add albums from the
catalog to the library and, for an administrator, look after the installation.

## Turn on agent access

Agents need an address where they can reach Shijhon. An administrator enters it on the
dashboard as the **Agent address** under **Server → Agent access**, and restarts Shijhon.
It can also be set in the configuration file as `agents.public_url`, or with
`SHIJHON_AGENTS__PUBLIC_URL`. Changing the address later signs connected agents out.

The address has to be HTTPS, with two exceptions:

- On the machine Shijhon runs on, `http://127.0.0.1:4533` works.
- On a home network, an administrator can turn on **Plain HTTP on a home network**
  (`agents.private_http`) and use an address such as `http://192.168.1.20:4533`. The
  Navidrome password at sign-in and the agent's tokens then travel unencrypted on that
  network. Some agent apps refuse plain HTTP anyway.

Agent apps that run in the cloud connect from their provider's servers, so for them
Shijhon has to be reachable from the internet over HTTPS.

### An HTTPS address with Tailscale

[Tailscale](https://tailscale.com) gives a machine an HTTPS address without a domain or
certificate of its own. With Tailscale running on the machine Shijhon runs on:

```sh
sudo tailscale serve --bg 4533
```

Shijhon is then at `https://<machine>.<tailnet>.ts.net` for devices on the same tailnet.
That is the address to enter on the dashboard. For agent apps that run in the cloud,
`sudo tailscale funnel --bg 4533` makes the address reachable from the internet. Funnel has
to be allowed for the tailnet first
([Tailscale Funnel](https://tailscale.com/kb/1223/funnel)).

### With a reverse proxy

The proxy has to keep the `Host` header and send these paths to Shijhon, including the
two at the root of the address:

- `/shijhon/mcp`
- `/shijhon/agents/*`
- `/.well-known/oauth-protected-resource/shijhon/mcp`
- `/.well-known/oauth-authorization-server/shijhon/agents`

Keep query strings, authorization headers and form bodies out of its logs.

## Connect an agent

1. Open **Playlists** on the dashboard. **AI agents** shows the address to add, such as
   `https://music.example.com/shijhon/mcp`.
2. Add that address in the agent app as an MCP server. Apps often call it a connector or a
   custom connector.
3. The app opens Shijhon's sign-in in the browser. Sign in with a Navidrome account and
   choose what the agent may do.

The agent never gets the password. Each account's connected agents are listed on
**Playlists**, where they can be revoked. Agents that support skills can use the
[playlist skill](../skills/shijhon-playlists/SKILL.md).

## Agent capabilities

What an agent can do depends on the [permissions](#permissions) it was given at sign-in.

- An agent can create a playlist from songs it chooses in the library and the catalog.
  Songs not yet in the library are added as placeholders.
- An agent can replace a playlist's songs while the playlist keeps its ID. Shijhon doesn't
  start this on its own. An agent app's scheduled tasks can run it regularly.
- An agent can remove songs from a playlist and change their order. Songs removed from a
  playlist in a music app stay out of later versions.
- An agent can list the account's Shijhon playlists, including those made on the dashboard
  or by another agent. Reading the songs of a playlist it didn't make needs the library
  permission, and the history permission when listening history chose them.
- An agent can create and manage discovery mixes, which are songs similar to the Last.fm
  listening history ([discovery playlists](playlists.md#discovery-playlists)).
- An agent can import a playlist from a pasted list, review the matches, publish it and
  complete missing songs later ([import a playlist](playlist-imports.md)).
- An agent can create a preview, then publish or discard it.
- An agent can read facts about songs as hints: year, genres, Last.fm tags and, with
  [sound analysis](sound-analysis.md), tempo, key and energy.
- An agent can read the new releases of the artists the account follows, once the
  account turned that on ([new releases](how-it-works.md#new-releases)).
- An agent can prepare a playlist's audio on the server
  ([prepare a playlist's audio](playlists.md#prepare-a-playlists-audio)).
- An agent can search the catalog and add albums from it to the account's library, or a
  limited number of an artist's releases. Each album is added whole and joins the
  account's Shijhon library, as an album the account rated or put into a playlist does
  ([what brings music in](collection.md#what-brings-music-in)). This needs the add
  permission.
- An agent of a Navidrome administrator can list the albums in the Library's review list
  and settle them one at a time: fill an album from one of the releases offered, keep it
  as it is, or match it again. This needs the server administration permission.
- An agent of a Navidrome administrator can read a summary of the server's status: the
  versions, whether Navidrome and the catalog answer, each add-on's state, how many albums
  wait for review, the cleanup's state and whether saved settings wait for a restart. It
  changes nothing, and it needs the server administration permission.

## Permissions

Permissions are chosen at sign-in and can be changed by connecting again.

| Permission | What it allows |
|---|---|
| Make and change playlists (`create`) | Create playlists and change the account's Shijhon playlists |
| Check playlists (`status`) | List the account's Shijhon playlists and check their progress |
| Library (`library`) | Read the library and playlists, including the songs of Shijhon playlists the agent didn't make |
| Listening history (`history`) | Read Last.fm listening counts, which discovery mixes also need. With the library permission, read the new releases of the artists the account follows, which come from its favorites and plays |
| Downloads (`downloads`) | Prepare playlist audio on the server |
| AI model (`settings`) | Choose the account's model for [server AI](playlists.md#server-ai) |
| Add to the library (`add`) | Search the catalog and add albums and artists' releases to the account's library |
| Server administration (`admin`) | For Navidrome administrators: settle the albums in the Library's review list and read the server's status |

Shijhon's instructions tell agents to use history or the library only when a request asks
for it.

Adding to the library and server administration change more than playlists do. Adding
writes whole albums into the library, which can use the server's disk once their audio is
played or prepared. Server administration acts for everyone who uses the installation.
The sign-in page therefore shows both cleared, also when the agent app asks for them, and
grants them only when you select them. An agent that was connected before these
permissions existed does not have them until it is connected again and they are selected.

Server administration is offered only to an account that is a Navidrome administrator when
it signs in. It also works only for one: Shijhon asks Navidrome at every call, so an agent
of an account that is no longer an administrator is refused, whatever was granted.

When an administrator has set a [daily cap on adding](how-it-works.md#the-daily-cap-on-adding),
the albums an agent adds count for its account like the account's own additions. The agent
is told how many were added and how many the limit left out.

## Revoking an agent

An agent is revoked on **Playlists**. That stops its unfinished work, including the matching
and publication of imports it started and the scheduled refreshes of playlists and discovery
mixes it made. Albums it was still adding are not added, and a review-list action it
started changes nothing more: a fill ends before it writes anything further, and a keep or
a match again that was still waiting leaves the album's match as it was. What it already
added or settled stays.
Playlists it already published stay, and can still be paused on the dashboard,
where their previews can also be discarded. What you do on the dashboard with an agent's
import or discovery mix, such as publishing it or asking for a new selection, is your own
work and goes on after the agent is revoked.

[Agent tools](reference/agent-tools.md) describes each tool, its limits and how requests
recover after a lost answer.
