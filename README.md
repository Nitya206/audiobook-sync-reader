# Audiobook Sync Reader

Turn an EPUB and its audiobook into one HTML file that reads along with the narrator. The page highlights the sentence being spoken, turns the page on its own, and lets you click any word to jump the audio there.

You bring your own book and audio. Nothing is uploaded anywhere. Transcription and alignment run on your Mac.

## What you get

| Feature | Detail |
|---|---|
| Read-along highlight | Sentence or paragraph, switchable in settings |
| Auto page turn | Follows the narrator, pauses when you turn a page by hand |
| Click to seek | Click a word, the audio jumps to it |
| Two-page mode | Side-by-side pages on a wide window |
| Chapter openers | Heading and synopsis line light up when they are read |
| Player | Speed, sleep timer, bookmarks, chapter list, resume where you stopped |
| Themes | Dark, light, auto, font, text size, line spacing, margins |
| Output | One self-contained `.html` file, no server, no install |

## Requirements

- macOS on Apple Silicon
- Python 3.8 or newer, no extra packages
- `ffmpeg`
- `macparakeet-cli`, which does the speech-to-text

```bash
brew install ffmpeg
brew install moona3k/tap/macparakeet-cli
```

The transcriber is Mac-only. On another system you can still build a book if you already have word timestamps, see `--words` below.

## Build a book

```bash
python3 audiobook_sync.py \
  --epub book.epub \
  --audio book.m4b \
  --template audiobook-reader-template.html \
  --out book.html
```

Open `book.html` in Chrome. Keep the audio file where it was when you built the book, and the reader loads it by itself. If it cannot find the audio, it asks you to pick the file.

A 13-hour audiobook takes about 6 minutes to transcribe on an M1. The last line printed is `DONE` or `FAILED`.

## Options

| Flag | Meaning |
|---|---|
| `--epub` | One or more EPUB files |
| `--audio` | One file, several files, or a folder. Several files are joined into one |
| `--template` | The reader template from this repo |
| `--out` | The HTML file to write |
| `--skin` | `shadow` (web-novel look, default) or `meridian` (printed-book look) |
| `--chapters` | A range such as `219-250`. Default is whatever the audio covers |
| `--title`, `--subtitle`, `--author` | Shown in the reader. Default comes from the EPUB |
| `--book-id` | Key for saved progress. Default comes from `--out` |
| `--words` | Reuse a word-timestamp JSON and skip transcription |
| `--no-repair` | Skip the second pass over stretches the transcriber dropped |

### Using your own transcriber

`--words` takes a JSON file in this shape:

```json
{ "wordTimestamps": [ { "word": "See", "startMs": 131360, "endMs": 131700 } ] }
```

Any speech-to-text tool that gives a start and end time per word can produce it.

## How the sync works

1. `macparakeet-cli` transcribes the audio with a time for every word.
2. The script pulls chapters and paragraphs out of the EPUB.
3. Book words are matched to spoken words. Stretches the transcriber dropped are cut out and transcribed again.
4. Word times are moved to the end of the silence before them, so highlights do not arrive early.
5. Everything is written into the template as one HTML file.

Work files and a `review.txt` report go to `.sync/<out name>/` next to the output. The report lists how much of each chapter matched. A rebuild reuses the cached transcript, so it takes seconds.

## Keys

| Key | Action |
|---|---|
| `→` `↓` `Space` | Next page |
| `←` `↑` | Previous page |
| `Alt` | Play or pause |
| `>` `.` | Next sentence |
| `<` `,` | Start of this sentence, then the one before |
| `B` | Bookmark this spot |
| `H` | Hide or show the bars |

## Limits

- Tested in Chrome on macOS. Other browsers may work but are untested.
- The EPUB and the audiobook must be the same edition. An abridged recording will match badly.
- Chapters need headings the script can find, such as "Chapter 12" or a bare number.

## Copyright

The HTML file the script writes contains the full text of the book. Build it for your own reading. Do not publish a built book unless you hold the rights to the text and the recording.

## Credits

- Speech-to-text by [MacParakeet](https://macparakeet.com), run through its CLI.
- The template embeds EB Garamond and IM Fell English, both under the SIL Open Font License 1.1.

## License

MIT, see `LICENSE`.
