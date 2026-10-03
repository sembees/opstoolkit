import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'
// ★ 样式引入顺序有意义：组件库样式 → 设计 token → 组件库变量桥 → 我们的全局样式。
//   element-bridge.css 必须排在 dist/index.css 之后，否则 :root 覆盖会被库盖回去。
import './styles/tokens.css'
import './styles/element-bridge.css'
import './styles/main.css'

const app = createApp(App)

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
