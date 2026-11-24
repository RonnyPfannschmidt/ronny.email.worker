<script>
  let { bundle } = $props();
</script>

{bundle.operation}{bundle.flag ? ` ${bundle.flag}` : ""}
{#if bundle.target_container}<span class="arrow">→</span>{bundle.target_container}{/if}
