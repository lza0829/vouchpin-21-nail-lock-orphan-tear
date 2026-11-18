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
async function quarantine() {
  msg.value = ''; err.value = ''
  try {
    const r = await api.quarantine({ business_day: preview.value.business_day })
    msg.value = `已将 ${r.quarantined_count} 笔锁后孤儿硬剔进冲突桶（不进任何批次），闭包重新闭合`
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
      <div class="stat"><div class="k">锁后孤儿 open</div>
        <div class="v" :class="{ err: preview.orphan_open_count > 0 }">{{ preview.orphan_open_count }}</div>
      </div>
      <div class="stat"><div class="k">冲突桶</div>
        <div class="v" :class="{ err: preview.conflict_count > 0 }">{{ preview.conflict_count }}</div>
      </div>
    </div>
    <div class="row">
      <button type="button" @click="seal"
        :disabled="preview.already_sealed || preview.open_count === 0">钉扎本日核销</button>
      <button type="button" class="ghost" @click="load">刷新预览</button>
      <button type="button" class="ghost" v-if="preview.orphan_open_count > 0" @click="quarantine">
        消化孤儿进冲突桶（{{ preview.orphan_open_count }}）
      </button>
    </div>
    <p class="msg" v-if="preview.already_sealed && preview.open_count === 0 && preview.orphan_open_count === 0">
      该业务日已钉扎闭包：无可钉 open，新核销一律 409 拒写，不存在可二次预览放大的行。
    </p>
    <p class="msg err" v-if="preview.orphan_open_count > 0">
      检测到 {{ preview.orphan_open_count }} 笔锁后孤儿（日已 sealed 却仍 open）：它们不会进入待钉列表、
      不可二次钉扎，必须先消化进冲突桶，闭包才算闭合。
    </p>
    <p class="msg" v-if="msg">{{ msg }}</p>
    <p class="msg err" v-if="err">{{ err }}</p>

    <div class="panel" v-if="preview.orphan_facts && preview.orphan_facts.length" style="margin-top:12px">
      <strong>锁后孤儿（不得当可钉 open，禁止再钉第二份现行）</strong>
      <table>
        <thead><tr><th>事实ID</th><th>券码</th><th>活动</th><th>金额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="f in preview.orphan_facts" :key="f.id">
            <td>{{ f.id }}</td><td>{{ f.coupon_code }}</td><td>{{ f.campaign_code }}</td>
            <td>{{ f.amount }}</td><td><span class="tag warn">{{ f.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="preview.conflict_facts && preview.conflict_facts.length" style="margin-top:12px">
      <strong>冲突桶（已硬剔，不进任何批次、不参与批头合计，仅审计）</strong>
      <table>
        <thead><tr><th>事实ID</th><th>券码</th><th>活动</th><th>金额</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="f in preview.conflict_facts" :key="f.id">
            <td>{{ f.id }}</td><td>{{ f.coupon_code }}</td><td>{{ f.campaign_code }}</td>
            <td>{{ f.amount }}</td><td><span class="tag warn">{{ f.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

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
  <div class="panel" v-if="preview && preview.facts.length">
    <table>
      <thead><tr><th>事实ID</th><th>券码</th><th>活动</th><th>金额</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="f in preview.facts" :key="f.id">
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
