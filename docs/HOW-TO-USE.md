# How to use Lead Desk

Lead Desk finds local businesses that need a **website**, a **WhatsApp reply bot** or a **website AI chatbot**,
shows you why each one is a fit, writes a first message for you to edit, and reminds you to follow up.
It never sends anything by itself: you copy the message and send it from your own WhatsApp, email or Instagram.

The whole loop is: **find → check → message → follow up → close**. The rest of this guide walks through it.

---

## 1. Start it

```powershell
uv run python -m leadgen.cli serve
```

Open http://127.0.0.1:8765. No password is needed on your own computer. You land on **Today**.

For AI drafts and ideas, Ollama must be running (it usually starts with Windows). Without it, everything else
still works and you can use **Template draft (no AI)** instead.

## 2. One-time setup (10 minutes, do this first)

Go to **Settings**:

1. **Sender profile**: your name, studio name, email, Instagram, portfolio link and signature. Every draft uses
   these, so fill them in properly (the default "Frame Studio" is a placeholder).
2. **Offers**: set a price on "Business website", "WhatsApp reply bot" and "Website AI chatbot". Without a price,
   drafts say "pricing depends on what you need, and I can give you a quote". That is fine, but a clear starting
   price ("from QAR 1,500") usually gets more replies.
3. **Customer profiles**: use **"Qatar businesses needing websites & AI assistants"**. It already looks for all
   three services in any industry. Only make a new profile if you want a narrower target, for example "Doha
   clinics" or "UAE restaurants".

## 3. Find leads (2–3 times a week)

### Discover (free, from OpenStreetMap)

1. **Find leads → Discover**.
2. Country `QA`, city `Doha` (or leave the city empty for all of Qatar), profile "Qatar businesses needing websites
   & AI assistants".
3. Keep the pre-ticked categories (restaurants, cafes, clinics, dentists, salons, gyms, garages, car washes), or tick
   other groups. One category at a time gives cleaner results than everything at once.
4. **Search OpenStreetMap**, then look at the preview:
   - Businesses **with a phone and no website** are at the top. These are your best website prospects, and you can
     reach them on WhatsApp.
   - "'website' is a facebook / zomato page" means they have no real site of their own: also a good prospect.
   - Grey, unticked rows are name-only (no way to contact them), chains, or already in your list. Leave them.
5. Tick the ones you want and add them. **Don't add hundreds.** 10–20 good ones per search is plenty for a week.

### Import (your own lists)

**Find leads → Import → Paste a list** for businesses you found yourself: Instagram handles, websites, or names
with phone numbers, one per line. This is the best way to add Instagram-only shops, which are rarely on the map.

### Research

On a lead that has its own website, press **Research website**. Lead Desk reads their site (politely, respecting
robots.txt) and checks: does it work on phones, is it secure (https), is the copyright year old, is there already a
chat widget, a WhatsApp button, online booking. **Research** in the menu does this in batches.

Leads with no website don't need research: having no site *is* the finding.

## 4. Pick who to contact (daily, 5 minutes)

Open **Leads** and sort by score, or use **Today → Best not yet contacted**.

Open a lead and look at **What to pitch**:

| You see | It means | Pitch |
|---|---|---|
| Website, high bar | No site, only a listing page, a broken site, or an old one that fails on phones | A new website |
| WhatsApp reply bot, high bar | They take customers on WhatsApp/phone and get many repeat questions (restaurants, clinics, salons, garages) | A bot that answers prices, hours, bookings 24/7 |
| Website AI chatbot | They have a working site with no chat on it | A chatbot on their site |

The **score** (0–100) is how good a fit they are, not a promise they will buy. Under it, **Evidence** shows exactly
what Lead Desk found and where. Items marked **Not verified** are guesses from missing data: check them yourself
before you mention them (for example, open Google Maps to confirm they really have no website).

## 5. Write and send the message

On the lead page, under **Outreach drafts**:

1. Choose the **Pitch** (it defaults to the best fit) and the channel. For Qatar, **WhatsApp** usually works best.
2. Press **Draft with AI** (about a minute on this laptop) or **Template draft (no AI)** (instant).
3. **Read and edit it.** Make it sound like you. Remove anything that isn't true or sounds odd, for example "I saw
   you on OpenStreetMap" (say "I came across your business" instead).
4. **Approve** it. For WhatsApp you get an **Open WhatsApp chat** link: it opens their chat, then paste the message
   (use **Copy message**) and send it yourself.
5. Back in Lead Desk, press **Mark as sent** and set a **follow-up date** (3–4 days is a good default).

Good habits:

- 10 personal messages a day beat 100 copy-paste ones.
- Mention one real, specific thing: "your menu is only on Talabat", "your site doesn't open properly on a phone".
- Ask a small question ("Can I send you a quick example?"), not for a meeting straight away.
- If someone says stop, open their lead and press **Mark do-not-contact**. Lead Desk then won't suggest them again.

## 6. Follow up and track (daily, 10 minutes)

**Today** is your to-do list. Work it top to bottom:

1. **Needs you now**: replies to answer and follow-ups that are due.
2. Approved drafts you haven't sent yet.
3. **Keep an eye on**: people you messaged who haven't replied.

When something happens, open the lead and use **Log activity**: replied, meeting booked, proposal sent, won, lost.
Move the lead in **Pipeline** as it progresses. One follow-up after 3–4 days, a second after a week, then stop.

## 7. Check what works (weekly, 5 minutes)

**Analytics** shows your funnel (found → contacted → replied → meeting → won), split by source. Use it
to answer: which service gets the most replies? Which business type? Is WhatsApp better than email?
Log your time in **Analytics → Time and cost** so you know how many hours each client costs you to win.

---

## A simple weekly routine

| When | What | Time |
|---|---|---|
| Monday | Discover one or two categories, add 15–20 good leads | 20 min |
| Every day | Today list, then 5–10 new messages from "Best not yet contacted" | 30 min |
| Every day | Log replies and set follow-ups | 5 min |
| Friday | Analytics, back up (`uv run python -m leadgen.cli backup`) | 10 min |

## Other pages

- **Outreach**: all drafts and sent messages in one place.
- **Duplicates**: when two leads look like the same business, merge them or mark them "Not a duplicate".
- **Audit log**: a record of everything that happened (searches, research, drafts, changes).

## Common questions

**It says "no website" but they have one.** OpenStreetMap is incomplete. Check Google quickly; if they do have a
site, edit the lead, add the website, and press **Research website**.

**The AI draft is slow or says "AI unavailable".** The local model needs free memory. Close some browser tabs, or use
**Template draft (no AI)**.

**"OpenStreetMap's free server is busy".** Wait a few minutes and search again. Repeating a search within 24 hours
uses the saved results.

**Can it send messages for me?** No, by design. You stay in control of everything that goes out.

More technical details (backups, restore, hosting): [RUNBOOK.md](RUNBOOK.md).
