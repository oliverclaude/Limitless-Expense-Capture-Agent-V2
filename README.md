# Limitless Expense Capture Agent, version 2

Email in, one claim at a time. A Hermes job runs the program every 30 minutes. The program reads the slip, writes the month spreadsheet, and sends the picture and the words itself. Chat only decides which program command to run.

Version 1 is a separate program and a separate mailbox prefix (`claim:`). This repo does not stop it.

## Program

`agent.py` is the program. One run reads the env file and exits. `poll` takes the newest `claimv2:` message. `reply` applies a chat answer or a settings change. `install-cron` records the Hermes job. Run that last one by hand, once. The bot does not run it, and nothing here runs `sudo`.

`mail.py` talks to the mailbox. It accepts a subject only when it starts with `claimv2:` (case is ignored), checks the sender and the body password, saves the photo or PDF, and files the message into processed, needs review, failed, or rejected.

`extract.py` sends the slip to the local vision model, then applies the expense rules in code: whose money it was, the journal, VAT, and the comment taken from the subject. It does not call xAI.

`crop.py` asks the vision model for the four corners of the paper. It makes a polygon from those points and pulls it into a rectangle, using the longer horizontal edge as the width and the longer vertical edge as the height. Pixels outside that rectangle are dropped. If the model returns no corners, the older OpenCV crop is used.

`pdf_proof.py` reads the text of a digital PDF. If the PDF is a scan, it renders a page and hands that picture to `crop.py`.

`sheet.py` is the ledger. Each month has one workbook, `YYYY-MM-Expenses.xlsx`, with the same columns as version 1. It also finds a duplicate row and can delete one.

`state.py` stores the claim that is waiting for a person. The file lives under `AGENT_STATE_DIR`, not in version 1's queue. The chat and the scheduled job do not share a transcript. This file is the handoff.

`done_mail.py` sends the Done or Skipped email to the work address in the env file. It does not reply to whatever address was on the claim.

`ntfy_out.py` sends the picture, then the words, on the version 2 topic. Both messages are tagged `hermes-agent` so the gateway does not treat them as a person typing.

`replies.py` matches only the duplicate sentences: "Keep both", "Remove the old one", and "Don't capture the new one". `ok` and `yes` are not a duplicate decision. Logging or dropping any other claim is not a phrase list. The chat agent reads the words and passes `--action confirm`, `skip`, `update`, or `ask`. The same file recognises a change to the high-value amount, a new journal, or the minutes between mail checks.

`settings.py` writes one key in the env file. The interval is also changed on the Hermes job, and that schedule is changed before the file, so a failed edit cannot leave the file ahead of the job.

## How it is started

`bin/expense-v2` is the chat command. It reads `AGENT_HOME` from the env file and runs `agent.py` with the repo's virtualenv. On the machine it is linked onto the gateway user's `PATH`.

`scripts/expense-v2-poll.py` is the scheduled entry. Hermes will not run a symlink that points outside its scripts directory, so `install-cron` copies this file to `~/.hermes/scripts/` and the job runs that copy. The copy only starts `agent.py poll`.

`skills/expense-v2/SKILL.md` is the Hermes skill. For a claim answer or a settings change it tells the model to run `expense-v2 reply` and then stop. The model does not send notifications, write the sheet, or run `poll`.

## Config and install

`templates/expense-agent.env` is the env file with the secrets left blank. On the machine it is `/etc/expense-agent-v2/env`, mode 600. The server address for notifications and for the vision model are filled in there, not in git.

`templates/ntfy.env` is the block appended to `~/.hermes/.env` so the gateway subscribes to `Fin-Limitless-V2`. The server address line is empty in git. Do not add email settings. The Hermes email adapter stays off.

`templates/hermes-gateway.service` shows the shape of the system unit. Do not copy it into place. `INSTALL.md` has the install command, which also turns on linger.

`INSTALL.md` is the install order: gateway first, then this program, the skill directory, and the one mail job.

`requirements.txt` is the Python packages for the virtualenv: image cropping, spreadsheets, and PDFs.

`.gitignore` keeps the virtualenv and compiled Python files out of git.

## Tests

`tests/test_rules.py` checks the subject prefix, the reply phrases, env edits, the spreadsheet row, and VAT. It does not open a mailbox and it does not call the vision model.
