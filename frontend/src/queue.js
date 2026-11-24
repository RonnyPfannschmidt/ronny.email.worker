import { mount } from "svelte";
import Page from "./Queue.svelte";

const target = document.getElementById("app");
mount(Page, { target, props: { csrf: target.dataset.csrf } });
