# Sound analysis with AudioMuse-AI

[AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) is an open-source tool that
listens to your library and describes how each song sounds. Connected to Shijhon, it gives
[described playlists](playlists.md) and [agents](agent-access.md) a few extra facts to
work with. It is optional: without it, nothing changes.

## What it adds

- **Facts about a song**: its tempo, key, style labels, how energetic it is, and how
  danceable, aggressive, happy, "party", relaxed and sad it sounds. Energy and those six
  are shown as **percentiles in your library**: 90 means louder or livelier than 90% of
  the songs AudioMuse-AI has analyzed for you, so a request such as "more energetic than my
  Morning playlist" has something to compare. Tempo is as AudioMuse-AI estimates it
  (sometimes half or double the beat you tap) and the key is an estimate too.
- **Songs that sound alike**: songs close to a few songs you name, and away from others.
- **Search by description**: songs that match words such as "warm piano, slow", optionally
  leaning toward or away from a few concepts AudioMuse-AI knows (instruments, moods, eras).
- **A path between two songs**: songs that move gradually from one to the other.

These are hints beside the AI's own knowledge of music, which stays the main way songs
are chosen. They are approximate, many songs have none, and a song without facts is never
left out because of it. When AudioMuse-AI is switched off or does not answer in time,
playlists are made without it.

## How playlists and agents use it

- **Described playlists**: the AI is shown the facts of the reference songs and of every
  candidate, as hints beside its own knowledge. With the **library context** option on, it
  may also describe the sound it wants in a few words. Shijhon then adds library songs
  that match the description, and library songs that sound like the reference songs, to
  the candidates, labelled as suggested by sound analysis. Without that option, no
  songs found by sound join and no library songs that nobody named reach the AI. See
  [facts as hints](playlists.md#facts-as-hints).
- **Agents** with the library permission can read the facts of songs and playlists and ask
  for similar songs, songs matching a description, or a path between two songs. See
  [connect an agent](agent-access.md#features-with-partial-configuration).

## Set it up

1. **Run AudioMuse-AI** (version 3.6 or later). Its own documentation covers
   installation. Do not start its analysis yet.
2. **Create a Navidrome account for it**, for example `audiomuse`, with access to your
   library. It only needs to read. Use it for nothing else: Navidrome's own web page is
   not filtered for it (see [accounts for sound analyzers](how-it-works.md#accounts-for-sound-analyzers)).
3. **Create an API token** in AudioMuse-AI (its `API_TOKEN` setting).
4. **Tell Shijhon**, in its configuration file (or the environment), and restart it. The
   account goes in the file or the environment only: the dashboard does not save it, and
   it applies after a restart.

   ```toml
   [placeholders]
   real_audio_accounts = ["audiomuse"]   # the account of step 2

   [audiomuse]
   url = "http://audiomuse:8000"
   token = "…"             # or set SHIJHON_AUDIOMUSE__TOKEN in the environment
   reach = "private"       # AudioMuse-AI usually runs on this machine or your network
   # server = ""           # the name AudioMuse-AI gives this Shijhon; empty: its default
   ```

   The `[audiomuse]` settings can also be saved on the dashboard's **Sound analysis** page (they
   apply after a restart). `reach` must match where AudioMuse-AI runs: `"loopback"` for
   the same machine (in a container, the container itself), `"private"` for your
   network, the Docker host and other containers. The default, `"public"`, refuses both.
   Leave `url` empty to switch sound analysis off.

   For the listed account, Shijhon's answers show only songs that have real audio (no
   silent placeholders of catalog songs, no catalog additions), and playing or
   downloading a placeholder never starts a delivery. Without the account in the list,
   AudioMuse-AI may analyze silent placeholders and spend its time on them; Shijhon
   ignores such analysis, but cannot stop AudioMuse-AI from doing the work. The setting
   is in the [settings reference](reference/settings.md#top-level-server-navidrome-placeholders).
5. **Sign in once as that account** at Shijhon's dashboard, `/shijhon/sign-in` (at the
   address your apps use), so that Shijhon prepares its library view. Until you do,
   Shijhon answers the account's listings with "Sign in once…" and AudioMuse-AI cannot read
   the library.
6. **Point AudioMuse-AI at Shijhon**, not at Navidrome: in AudioMuse-AI, add a Navidrome
   media server with Shijhon's address (the one your apps use) and the **user name and
   password** of the account. Do not use an API key: Navidrome does not accept one, so the
   account would not be recognized. Then start the analysis. Large libraries take a while,
   and Shijhon picks up songs as they are analyzed.

The **Sound analysis** page then shows whether AudioMuse-AI answers, how many songs have facts and
when they were last read. It is for Navidrome administrators, like the other settings
pages.

## What Shijhon reads

After startup (once the facts are older than the interval) and then every `sync_hours`
(6), Shijhon asks AudioMuse-AI which songs it has analyzed and fetches the analysis of the
new and changed ones. The facts are kept in Shijhon's database under the songs'
Navidrome IDs; a song AudioMuse-AI no longer has, or that is a silent placeholder, loses
its facts. Reading stops when Shijhon stops and goes on from where it was.

## Privacy

Between Shijhon and AudioMuse-AI go Navidrome song IDs, the analysis, the words of a
search by description, and the token (as a bearer token, only to the configured
address; never shown or logged). Titles, listening history and accounts are not sent.
AudioMuse-AI reads your library on its own from the address in step 6. When facts are
used in a playlist request, they are part of what the request's AI service is shown about
those songs, like their year or genre.

## Limits

- Songs without real audio have no facts: catalog songs that are only placeholders, and
  songs AudioMuse-AI has not reached yet.
- The six mood-like scores need AudioMuse-AI's text-and-audio model (CLAP); until it has
  analyzed a song, that song has only the other facts. Search by description needs it too,
  and leaning toward or away from concepts needs its steering models.
- Percentiles compare songs of your library with each other, so they shift as the library
  changes.
- Songs are described by how they sound, not by what they are: a live version and the studio
  version can differ.

## When something is wrong

The **Sound analysis** page says why, in a few fixed sentences:

- *did not accept the token*: create a new token in AudioMuse-AI and enter it again.
- *the token was entered for another address*: the address changed (on the dashboard, in
  the file or by the environment) after a token was saved for the old one. Enter the token
  again on the **Sound analysis** page; nothing is sent to AudioMuse-AI until then.
- *the network setting does not allow this address*: set `reach` (step 4).
- *cannot be reached*, *took too long to answer*: check that AudioMuse-AI runs and that
  `url` is the address Shijhon can reach from where it runs.
- *does not offer what Shijhon asks*: AudioMuse-AI is older than 3.6.

If AudioMuse-AI itself reports that it cannot read the library, or no song gets facts,
check that the account signed in once (step 5) and is listed in `real_audio_accounts`
(step 4), and that AudioMuse-AI uses its user name and password.

The settings are in the [configuration guide](configuration.md#audiomuse).
