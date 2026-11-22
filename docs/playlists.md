# Playlists

To keep an existing ordered track list as it is, including repeated songs,
[import it](playlist-imports.md): paste artist/title lines or upload a playlist file,
review the matches, then publish.

## Agents

The main way to make playlists is your own AI agent, connected over MCP
([agent access](agent-access.md)). It chooses the songs itself, from a request such as
"make a calm evening playlist from my library", publishes them to Navidrome, and can
refresh or change the playlist later. It needs no AI connection on the server.

The rest of this page covers the dashboard's own tools: discovery mixes from your
listening history, described playlists made by [server AI](#server-ai), which is
optional, and preparing a playlist's audio. The [detailed rules](reference/playlists.md)
cover how these choose, publish and retry.

## Describe a playlist

Open **Playlists → Describe a playlist** in the dashboard. It needs
[server AI](#server-ai); Last.fm is optional. Enter the instructions, the number of
tracks (1–100) and the most tracks per artist (1–10), then choose **Create playlist**.
The playlist is published to Navidrome right away. To look at it first, check **Review a
preview before publication** under **Advanced**, and nothing is published until you choose
**Publish playlist**. Signing in connects your Navidrome account. If you disconnect it,
connect it again on **Playlists**.

The name is optional. Left blank, Shijhon chooses a short title from your instructions
and the chosen music, and refreshes keep that title. A name you enter is kept exactly.

Shijhon asks the AI for artists and titles, looks them up in your library and the
catalog, and has the AI choose and order only songs it found. An album or version named
in the instructions has to match: a version that cannot be verified fails rather than
taking a different remaster. If too few songs fit the count and the artist limit, the
request fails and says so instead of ignoring your instructions. When the instructions
ask for flow, the AI is asked for an order that uses tempo, key and energy where they are
known. With [AudioMuse-AI](sound-analysis.md) connected, facts about how songs sound are
one more hint beside the AI's own knowledge of the music.

Two options, **Use my listening history** and **Use my library as context**, are off by
default. History gives the AI up to 100 artist/title listening counts, without a timeline,
and needs the Last.fm connection described under [discovery
playlists](#discovery-playlists). Library context shares a sample of up to 100 songs from
your library. With both off, the AI sees your instructions, its own suggestions, the songs
found for this request with their [facts](#facts-as-hints), and the songs of any
[references](#reference-playlists-and-songs).

The request's page shows what the AI worked with and what it chose:

- the references it compared with, each with a short profile (typical energy and tempo,
  years, genres, tags);
- a one-sentence goal for the playlist;
- for each song, the AI's reason, whether it fits, partly fits or doesn't fit, where it
  comes from (your files, added by Shijhon or the catalog), whether sound analysis
  suggested it, and its year, genres, tags and key;
- with sound facts, each song's energy and tempo beside the reference's. Energy is a
  percentile in your library.

When an administrator has set a [daily cap on
adding](how-it-works.md#the-daily-cap-on-adding), a playlist is published without the
songs whose releases would pass your cap, and its page says how many were left out.

Requests are one-off by default. Check **Save these instructions for reuse** to refresh
the playlist by hand from its page. With that box checked, a **Regular refresh** every
day or every week also works, starting after the first publication. **Pause refreshes**
stops future refreshes. On the dashboard, you can pause a playlist an agent created and
discard its preview, even after the agent's connection is revoked. A retry
publishes the same selection again instead of choosing different music or creating a
second playlist.

### Reference playlists and songs

A description can relate to music you already have: "like my Velvet Orbit playlist but
more energetic". In the dashboard, **Compare with your playlists** lists your own
Navidrome playlists; choose up to three. Without a choice there, a playlist named in the
instructions is used, such as `my Driftwood playlist` or a name in quotes. The request
shows the references used and whether they were found by name.

**Leave out songs already in the reference** is checked by default and keeps the songs of
the playlists you chose out of the new one. Songs of a playlist found by name are not
removed, but the AI is asked to avoid repeating them unless the instructions ask for
them. References are read again at every refresh, so refreshes follow their current
songs. The [detailed rules](reference/playlists.md#references) cover how names are
matched, how many songs are read and what agents need.

### Facts as hints

For reference songs and candidates, the AI also sees a few facts in case they help:

- year and genres, as your library or the catalog gives them;
- up to five Last.fm community tags, when your playlist connection has a Last.fm API key;
- tempo, key, energy and mood percentiles from [sound analysis](sound-analysis.md) with
  AudioMuse-AI, when it is set up and has analyzed the song's audio;
- where the song comes from: your own files, added to the library by Shijhon, or the
  catalog (not in the library yet).

Facts are approximate, many songs have none, and the AI's own knowledge of the music comes
first. A missing fact never excludes a song, and an unavailable source never fails the
playlist. With sound analysis set up and **Use my library as context** on, the AI can also
describe the sound it wants. Library songs that match that description, or sound like the
reference songs, then join the candidates, labeled as suggested by sound analysis.

### Your edits to a refreshed playlist

Songs you remove from a refreshed playlist stay out of its later refreshes,
and the AI is told you removed them, so it can avoid similar choices. Songs you add are
shown to the AI as examples of what you want, but the refresh replaces them too. Your
edits teach only that playlist's refreshes, and each refresh's result shows what it
learned. A comment or visibility you set stays.

Renaming the playlist in Navidrome stops the next refresh: give it its earlier name, or
pause its refreshes to keep the new name.

The request page of a described playlist and the page of a discovery mix list the songs
Shijhon remembers from your edits. **Forget learned edits** (also the agents'
`forget_edits` refresh action) clears them for that playlist: removed songs can be
suggested again and added songs stop guiding the AI. Your playlist in Navidrome does not
change, and edits made since its last refresh are learned again at the next one.

## Discovery playlists

A discovery mix is a playlist of songs similar to what you listen to, from your Last.fm
history. Open **Playlists** in the dashboard and, under **Connection and AI settings**,
enter your Last.fm username. By default, Shijhon reads the Last.fm API key configured in
Navidrome. To read it, Shijhon uses the Navidrome password you enter when you save the
username. If that key is unavailable, set **Last.fm API key source** to **An API key**
and enter your own.

Signing in already connects your Navidrome account. After you disconnect, connect again
there with your password. Changing the Navidrome password ends that authorization, so
sign in or reconnect afterward.

In apps, the same key adds Last.fm's lists to
[radio and similar artists](how-it-works.md#radio-from-catalog-items) for music that is not
in your library. ListenBrainz's lists need no key, and are used unless an administrator
turned ListenBrainz off.

Fill in **New discovery mix** and choose **Create and publish**, then listen to the
playlist in your music app. Publishing can add whole releases as
[placeholders](how-it-works.md#placeholders), or missing tracks to a matched album, but it
does not download audio. **Track and release details**
on the mix's page lists them.

The defaults are 30 tracks, at least 70% unfamiliar music and two tracks per artist.
Music you listened to in the last 14 days is left out, and so are this mix's tracks from
the last 28 days. **Advanced settings** holds the unfamiliar share, the artist limit and
those two periods.
"Unfamiliar" means absent from your imported history, not that you have never heard a
track. A brief such as "melodic, adventurous music for an evening walk" needs **Use AI
curation** and [server AI](#server-ai). The first import covers 90 days of Last.fm history
by default, and later imports add new listens.

Candidates are songs that Last.fm lists as similar to the songs you played most, and
songs that ListenBrainz lists as listened together with them (unless an administrator
turned ListenBrainz off). If too few recordings meet the rules, the run fails, and you can
adjust the rules before generating again.

To approve each selection first, select **Review before publishing** under **Advanced
settings**. New selections and scheduled refreshes then wait on the mix's page until you
choose **Publish playlist**, and **Generate preview** always stops at a preview.

Regular refreshes are optional. Choose every day or every week and check **Enable regular
refreshes after publishing**, or enable them later on the mix's page. A refresh runs a day
or a week after the last publication, while Shijhon is running, and keeps the playlist's
ID. Songs you remove from or add to the playlist teach its next refresh, as for
[described playlists](#your-edits-to-a-refreshed-playlist), and renaming the playlist
stops the next refresh.

A failed publication or scheduled run pauses the mix. Choose **Retry publication** to
publish the same selection, and enable refreshes again if needed.

## Prepare a playlist's audio

After publication, open **Downloads** to prepare the audio of a native playlist in the
server library, so its songs are ready before anyone plays them. This works for any native
playlist your account owns. The page can use your discovery connection, or connect
separately with your Navidrome password and the playlist you choose. Reconnect if the
password changes. Preparing audio does not edit the playlist, add releases or choose new
recordings. Songs you own or that are already downloaded need nothing fetched. A playlist
can have as many entries as a playlist import (50,000 by default).

The page shows each song's progress and failures, with the first 50 songs listed. Songs
wait for their turn at the add-ons, also while an add-on is rate limited. Refresh the
page to read current progress. **Cancel** stops queued and waiting songs and lets
transfers under way finish. Completed files remain. After a restart, unfinished
preparation is paused: review it and choose **Resume**.

This prepares files on the server and does not put copies on your devices: use your
app's own offline-save feature afterward. The limits, and how apps' own downloads are
handled, are in [Add-ons](add-ons.md#downloads-and-playlist-preparation).

## Server AI

Server AI is an optional AI model that Shijhon calls itself, on your machine or through
a remote API. Playlists described on the dashboard and their scheduled refreshes need it,
and it can curate discovery mixes. Without it, Shijhon chooses a mix's songs from
listening and similarity evidence. An agent you connect works without it. The brief and
the candidate songs are sent to the model, never account credentials or your listening
timeline. An AI failure stops the run instead of publishing a mix that ignores the brief.

Each account chooses in **Connection and AI settings → Server AI**: no AI, the
administrator's default model, or another model the administrator approved. The choice
applies on all of that account's devices, to described playlists, discovery curation and
future refreshes.

Administrators configure the connection in the same section: the API base URL and key,
the default model and up to 50 other models people can choose (one per line). Saving
changes the shared service for all accounts, and **No AI** turns it off. Approve only
models whose access and cost the installation can support.

The API has to accept `POST <base>/chat/completions` with `response_format: json_schema`,
which most providers and local AI servers do. Use a base such as
`https://your-endpoint.example/v1`. Public addresses need HTTPS. For AI on this machine or
your network, an administrator turns on **Allow AI on this machine or your network**
(`playlists.allow_private_api`). With Docker, the machine itself is
`host.docker.internal`, such as `http://host.docker.internal:11434/v1`, and the AI service
has to listen on more than `127.0.0.1` for the container to reach it.

The connection can also be Codex CLI or Claude Code on the server. They need more setup
([local AI runners](reference/local-ai-runners.md)). History and run time limits are set
in the configuration file ([limits](reference/playlists.md#limits)).

## Privacy and current scope

Playlist connections, imported history, requests and their results belong to your
Navidrome account and are not shared with other accounts. Shijhon keeps a reusable
authorization, never your password, and secret fields are never shown again once saved.
**Forget my playlist settings and history** forgets that account's connections and
history, and the settings and runs of its Shijhon playlists. Its Navidrome playlists and
library files remain. It also clears
the saved Downloads authorization, which pauses unfinished preparation until you reconnect
on **Downloads** and choose **Resume**. The [detailed
rules](reference/playlists.md#privacy-and-storage) say how this is stored.
