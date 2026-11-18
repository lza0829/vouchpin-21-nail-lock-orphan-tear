<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'
const rows = ref([])
const err = ref('')
const openId = ref(null)
const detail = ref(null)
const detailErr = ref('')

async function load() {
  try { rows.value = await api.batches() } catch (e) { err.value = e.message }
}
async function toggle(id) {
  detailErr.value = ''
  if (openId.value === id) { openId.value = null; detail.value = null; return }
  openId.value = id
  detail.value = null
  try { detail.value = await api.batchDetail(id) } catch (e) { detailErr.value = e.message }
}
onMounted(load)
</script>
<template>
  <div class="panel">
    <p class="msg err" v-if="err">{{ err }}</p>
    <table>
      <thead><tr><th>批次</th><th>业务日</th><th>状态</th><th>笔数</th><th>合计</th><th>钉扎时刻</th><th></th></tr></thead>
      <tbody>
        <template v-for="b in rows" :key="b.id">
          <tr>
            <td>{{ b.id }}</td>
            <td>{{ b.business_day }}</td>
            <td><span class="tag ok">{{ b.status }}</span></td>
            <td>{{ b.fact_count }}</td>
            <td>{{ b.total_amount }}</td>
            <td>{{ b.sealed_at }}</td>
            <td><button type="button" class="ghost" @click="toggle(b.id)">{{ openId===b.id ? '收起' : '明细/对账' }}</button></td>
          </tr>
          <tr v-if="openId===b.id">
            <td colspan="7">
              <p class="msg err" v-if="detailErr">{{ detailErr }}</p>
              <div v-if="detail">
                <p class="msg">
                  对账：批头 {{ detail.reconcile.header_count }} 笔 / {{ detail.reconcile.header_total }}，
                  明细 {{ detail.reconcile.detail_count }} 笔 / {{ detail.reconcile.detail_total }}，
                  <span :class="detail.reconcile.matches ? 'tag ok' : 'tag warn'">
                    {{ detail.reconcile.matches ? '一致' : '不一致' }}
                  </span>
                </p>
                <table>
                  <thead><tr><th>事实ID</th><th>券码</th><th>活动</th><th>金额</th><th>状态</th></tr></thead>
                  <tbody>
                    <tr v-for="f in detail.detail" :key="f.id">
                      <td>{{ f.id }}</td>
                      <td>{{ f.coupon_code }}</td>
                      <td>{{ f.campaign_code }}</td>
                      <td>{{ f.amount }}</td>
                      <td><span class="tag ok">{{ f.status }}</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>
