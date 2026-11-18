<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const msg = ref('')
const err = ref('')
const snapshot = ref([])

async function load() {
  err.value = ''
  try { preview.value = await api.preview() } catch (e) { err.value = e.message }
}
async function seal() {
  msg.value = ''; err.value = ''; snapshot.value = []
  try {
    const r = await api.seal({})
    snapshot.value = r.snapshot || []
    const codes = snapshot.value.map(s => s.coupon_code).join('、')
    msg.value = `已钉扎批次 #${r.batch_id} · ${r.fact_count} 笔 · 合计 ${r.total_amount}` +
      (codes ? ` · 锁前快照券码：${codes}` : '')
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div class="panel" v-if="preview">
    <div class="grid">
      <div class="stat"><div class="k">业务日</div><div class="v">{{ preview.business_day }}</div></div>
      <div class="stat"><div class="k">待钉笔数</div><div class="v ticket">{{ preview.open_count }}</div></div>
      <div class="stat"><div class="k">待钉合计</div><div class="v ticket">{{ preview.open_total }}</div></div>
      <div class="stat"><div class="k">已钉扎</div><div class="v">{{ preview.already_sealed ? '是' : '否' }}</div></div>
    </div>
    <div class="row">
      <button type="button" @click="seal" :disabled="preview.already_sealed || preview.open_count===0">钉扎本日核销</button>
      <button type="button" class="ghost" @click="load">刷新预览</button>
    </div>
    <p class="msg" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>
    <div class="panel" v-if="snapshot.length" style="margin-top:12px">
      <strong>锁前快照（仅含持锁前已 open 事实，券码/金额与批明细对账）</strong>
      <table>
        <thead><tr><th>事实ID</th><th>券码</th><th>金额</th></tr></thead>
        <tbody>
          <tr v-for="s in snapshot" :key="s.fact_id">
            <td>{{ s.fact_id }}</td><td>{{ s.coupon_code }}</td><td>{{ s.amount }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  <div class="panel">
    <table>
      <thead><tr><th>事实ID</th><th>券码</th><th>活动</th><th>金额</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="f in (preview?.facts || [])" :key="f.id">
          <td>{{ f.id }}</td>
          <td>{{ f.coupon_code }}</td>
          <td>{{ f.campaign_code }}</td>
          <td>{{ f.amount }}</td>
          <td><span class="tag warn">{{ f.status }}</span></td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
