<script>
  import { onMount } from "svelte";
  import { page } from "$app/state";
  import { cache, missing, oneAtATime, read } from "#lib/api.js";
  import Filing from "#lib/Filing.svelte";
  import MessageText from "#lib/MessageText.svelte";
  import { when } from "#lib/when.js";

  const messageId = Number(page.params.id);
  const MESSAGE = `/api/mail/message/${messageId}`;

  let shown = $state(cache.get(MESSAGE) ?? null);
  let error = $state("");

  const message = $derived(shown?.message);

  const load = oneAtATime(async () => {
    try {
      shown = await read(MESSAGE);
      error = "";
    } catch (problem) {
      error = missing(problem, "such message");
    }
  });

  onMount(load);
</script>

<svelte:head><title>{message?.subject || "message"} — mailmind</title></svelte:head>

{#if error}<div class="banner error">{error}</div>{/if}

{#if !shown}
  <p class="muted">loading</p>
{:else}
  <h1>{message.subject || "(no subject)"}</h1>
  <p class="small muted">
    {message.from_display_name ?? ""} &lt;{message.from_address ?? "—"}&gt; · {when(message.date)}
    {#if shown.container}
      · in <a href="/mail/{shown.container.id}">{shown.container.name}</a>
    {/if}
    {#if message.flags.length}· {message.flags.join(" ")}{/if}
  </p>

  {#each message.assessment.assessment as assessment}
    {#each assessment.findings as finding}
      <span class="finding {finding.class}">{finding.detail}</span>
    {/each}
  {/each}

  <MessageText {messageId} />

  {#if shown.container}
    <Filing containerId={shown.container.id} folders={shown.folders} picked={[messageId]} them="it" />
  {/if}
{/if}
