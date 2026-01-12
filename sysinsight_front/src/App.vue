<script setup lang="ts">
  import { RouterView } from 'vue-router';
</script>

<template>
  <!-- <h1>You did it!</h1>
  <p>
    Visit <a href="https://vuejs.org/" target="_blank" rel="noopener">vuejs.org</a> to read the
    documentation
  </p> -->
  <router-view />
</template>

<style scoped>
  :root {
  font-family: "Microsoft Yahei", sans-serif;
  background-color: #f5f7fa;
}
body {
  margin: 0;
  padding: 20px;
}
</style>
