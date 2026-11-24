<script>
  import { day } from "./when.js";

  let { bundle, folders, noun, grown, closed } = $props();
  const named = $derived(bundle.queries.find((query) => !query.grew));
  const later = $derived(bundle.queries.filter((query) => query.grew));
  const stale = $derived(bundle.items.filter((item) => item.status === "stale").length);
</script>

{#if closed}
  <div class="banner error">
    This bundle was closed while this page was open: {bundle.status}{bundle.decision_reason
      ? ` — ${bundle.decision_reason}`
      : ""}. There is nothing left to decide.
  </div>
{/if}
{#if grown}
  <div class="banner error">
    More {noun} arrived in this bundle after this page was drawn. Accepting it from here will be
    refused. <a href="/bundle/{bundle.bundle_id}">Read it again</a> to see them.
  </div>
{/if}
{#if bundle.target_is_new}
  <div class="banner">
    <strong>{bundle.target_container}</strong> does not exist yet. Accepting this is what makes it:
    the folder is created on the server immediately before the first message moves into it. If the
    folder cannot be made, nothing moves.
  </div>
{/if}
{#if folders}
  <div class="banner">
    Only empty folders are offered — the one removal that cannot lose mail — and each is checked
    again on the server just before it goes; one that has since received mail is left alone.
  </div>
{/if}
{#if named}
  <div class="banner">
    This bundle was found by searching for <strong>{named.text}</strong>, once, at proposal — the
    list below is the whole of it, and the search is not run again.
  </div>
{/if}
{#if later.length}
  <div class="banner">
    This bundle has been added to since it was proposed.
    {#each later as query}
      <strong>{query.added}</strong> of the {noun} below arrived from a search for
      <strong>{query.text}</strong> on {day(query.at)}.{" "}
    {/each}
    They are marked. Accepting this accepts them along with the rest, so read them first.
  </div>
{/if}
{#if bundle.self_assessed_by.length}
  <div class="banner">
    Assessed by <strong>{bundle.self_assessed_by.join(", ")}</strong> — the same producer that
    proposed this; weigh it accordingly.
  </div>
{/if}
{#if stale}
  <div class="banner">
    {stale} of these changed since this was proposed. They will not be applied. What changed is
    shown against each one below.
  </div>
{/if}
