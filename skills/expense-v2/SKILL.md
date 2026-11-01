---
name: expense-v2
description: "Limitless expense claims on the Fin-Limitless-V2 chat. Use when the user replies about a waiting claim in ordinary words, approves it, rejects it, corrects a field, answers a duplicate, or changes the high-value amount, a journal, or how often mail is checked."
version: 1.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [expense, claims, ntfy]
---

# Expense capture, version 2

The program does the work. You decide which command to run, then you stop talking.

## When to run it

Run the command when the message is about a waiting claim or a setting.

For a claim that is not a duplicate, you decide the action from the meaning of their words. No particular phrase is required. Do not rewrite what they said into ok, yes, or log.

- confirm: they want it logged. "That is 100%. Please log it." and "The claim is good to continue please." are confirm. So is any other ordinary approval. A high-value amount is not a reason to refuse once they approve.
- skip: they do not want it logged.
- update: they correct a field and do not say whether to log it. Pass only the fields they changed.
- ask: it is not an approval, a rejection, or a correction.

"My half" means personal is half the spend and company is 0. Pass those two amounts with the action their words call for.

A duplicate is different. Do not pass `--action`. Pass one of these sentences through unchanged, and no other wording:

- Keep both
- Remove the old one
- Don't capture the new one

Also run it, with no `--action`, to change the high-value amount, add a journal by name, or set how many minutes between mail checks.

For any other conversation, answer normally. Do not run the command.

## Command

Put flags before `--text`. `--text` is their words, unchanged, and it is last. On the terminal tool set `timeout` to 600.

```bash
expense-v2 reply --action confirm --text 'The claim is good to continue please.'
expense-v2 reply --action skip --text 'Do not log this one.'
expense-v2 reply --action update --merchant 'Woolworths' --text 'The merchant is Woolworths.'
expense-v2 reply --text 'Keep both'
```

Field flags, only when they named a new value: `--merchant`, `--comment`, `--journal`, `--claim-date`, `--personal`, `--company`, `--vat`.

## After the command

If the command exits 0, your entire final answer is exactly `[SILENT]`.

If it prints that it needs `--action`, run it once more with confirm, skip, update, or ask. Do not answer `[SILENT]` on that failure. The claim was not saved.

The program already sent the ntfy message when it exits 0. Do not repeat it. Do not send ntfy yourself. Do not invent a curl command. Do not run sudo. Do not run `expense-v2 poll`. The schedule does that.

## What you still do not do

You do not choose a duplicate. The program only accepts the three sentences, and it asks again otherwise. You still answer `[SILENT]`.

You do not write the spreadsheet, move mail, or delete proofs. The program does that.
