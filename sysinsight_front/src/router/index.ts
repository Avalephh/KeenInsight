import { createRouter, createWebHistory } from 'vue-router'
import LoadSelect from '@/views/LoadSelect.vue'
import SystemStatus from '@/views/SystemStatus.vue'
import ExceptionAnalysis from '@/views/ExceptionAnalysis.vue'
import KnobRecommend from '@/views/KnobRecommend.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'load-select',
      component: LoadSelect,
    },
    {
      path: '/systemstatus',
      name: 'system-status',
      component: SystemStatus,
    },
    {
      path: '/exceptionanalysis',
      name: 'exception-analysis',
      component: ExceptionAnalysis,
    },
    {
      path: '/knobrecommend',
      name: 'knob-recommend',
      component: KnobRecommend,
    }
  ],
})

export default router
