<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const d = ref(null)
const err = ref('')
async function load() {
  err.value = ''
  try { d.value = await api.dashboard() } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
<template>
  <div class="panel">
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">业务日</div><div class="v">{{ d.business_day }}</div></div>
      <div class="stat"><div class="k">今日事件</div><div class="v">{{ d.event_count }}</div></div>
      <div class="stat"><div class="k">核销事实</div><div class="v ticket">{{ d.fact_count }}</div></div>
      <div class="stat"><div class="k">待钉 open</div><div class="v">{{ d.open_fact_count }}</div></div>
      <div class="stat"><div class="k">已钉 sealed</div><div class="v ticket">{{ d.sealed_fact_count }}</div></div>
      <div class="stat"><div class="k">冲突桶</div>
        <div class="v" :class="{ err: d.conflict_fact_count > 0 }">{{ d.conflict_fact_count }}</div>
      </div>
      <div class="stat"><div class="k">今日核销额</div><div class="v ticket">{{ d.redeem_total_today }}</div></div>
      <div class="stat"><div class="k">日已钉扎</div><div class="v">{{ d.day_sealed ? '是' : '否' }}</div></div>
      <div class="stat"><div class="k">锁后仍 open（孤儿）</div>
        <div class="v" :class="{ err: d.orphan_open_count > 0 }">{{ d.orphan_open_count }}</div>
      </div>
      <div class="stat"><div class="k">闭包对账</div>
        <div class="v" :class="{ ok: d.sealed_reconciles, err: !d.sealed_reconciles }">
          {{ d.sealed_reconciles ? '闭合' : '未闭合' }}
        </div>
      </div>
    </div>
    <p class="msg" v-if="!err">钉扎事务持业务日写锁全程互斥：锁中核销阻塞、锁释放后日已 sealed 再核销返回 409 且整体回滚；日已钉后不得残留任何可钉 open，锁后孤儿须在核销页消化进冲突桶。</p>
    <p class="msg err" v-if="d && d.orphan_open_count > 0">
      检测到 {{ d.orphan_open_count }} 笔锁后孤儿：闭包未闭合，请先到核销页消化，禁止重复钉扎。
    </p>
    <p class="msg err" v-if="err">{{ err }}</p>
  </div>
</template>
