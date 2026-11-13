<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, postJSON } from '../api'
const datasets = ref([])
const datasetId = ref(1)
const busy = ref(false)
const err = ref('')
const last = ref(null)
onMounted(async () => {
  datasets.value = await getJSON('/api/datasets')
  if (datasets.value.length) datasetId.value = datasets.value[0].id
})
async function run() {
  busy.value = true
  err.value = ''
  try {
    last.value = await postJSON('/api/inspect', { dataset_id: Number(datasetId.value) })
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="page">
    <header>
      <h1>入检台</h1>
      <p class="muted">装载夹具 → 执行规则 → 隔离失败行 → 放行通过行到精选区</p>
    </header>
    <div class="panel row">
      <label>数据集
        <select v-model="datasetId">
          <option v-for="d in datasets" :key="d.id" :value="d.id">{{ d.id }} · {{ d.name }}</option>
        </select>
      </label>
      <button :disabled="busy" @click="run">{{ busy ? '入检中…' : '开始入检' }}</button>
    </div>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <div class="panel" v-if="last">
      <h3>批次 #{{ last.id }} <span class="tag" :class="last.status === 'released' ? 'ok' : ''">{{ last.status }}</span></h3>
      <div class="tiles">
        <div class="tile"><div class="hero-num">{{ last.scanned_count }}</div><div class="muted">扫描</div></div>
        <div class="tile"><div class="hero-num">{{ last.passed_count }}</div><div class="muted">通过</div></div>
        <div class="tile"><div class="hero-num">{{ last.quarantined_count }}</div><div class="muted">隔离</div></div>
        <div class="tile"><div class="hero-num">{{ last.released_count }}</div><div class="muted">签发</div></div>
        <div class="tile"><div class="hero-num">{{ last.report.counts.waived_hits ?? 0 }}</div><div class="muted">豁免命中（{{ last.report.counts.waived_rows ?? 0 }} 行）</div></div>
      </div>
      <div class="panel" v-if="last.report.waiver?.active_waivers?.length" style="margin-bottom:12px">
        <h4>本批生效豁免（判定时刻 {{ last.report.waiver.effective_at }}）</h4>
        <table>
          <thead><tr><th>豁免单</th><th>规则</th><th>实体键</th><th>有效期</th></tr></thead>
          <tbody>
            <tr v-for="w in last.report.waiver.active_waivers" :key="w.id">
              <td>#{{ w.id }}</td>
              <td>规则 {{ w.rule_id }}</td>
              <td><code v-if="w.entity_key">{{ w.entity_key }}</code><span v-else class="tag">全部实体</span></td>
              <td class="muted">{{ w.starts_at }} → {{ w.ends_at }}{{ w.note ? `（${w.note}）` : '' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <h4>对账</h4>
      <table>
        <thead><tr><th>校验</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in last.report.checks" :key="c.name">
            <td>{{ c.name }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '通过' : '失败' }}</span></td>
            <td class="muted">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
      <h4>行清单</h4>
      <table>
        <thead><tr><th>#</th><th>键</th><th>处置</th><th>命中</th></tr></thead>
        <tbody>
          <tr v-for="r in last.rows" :key="r.id">
            <td>{{ r.row_no }}</td>
            <td>{{ r.entity_key }}</td>
            <td><span class="tag" :class="r.disposition === 'pass' ? 'ok' : 'danger'">{{ r.disposition }}</span></td>
            <td class="muted">
              <template v-if="r.rule_hits.length">
                <span v-for="(h, i) in r.rule_hits" :key="i">
                  <span :class="h.waived ? 'tag ok' : ''">{{ h.name }}<template v-if="h.waived">（豁免 #{{ h.waiver_id }}）</template></span><template v-if="i < r.rule_hits.length - 1">；</template>
                </span>
              </template>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="panel" v-if="last.report.waiver?.waived_hits?.length" style="margin-top:12px">
        <h4>被豁免命中审计（{{ last.report.waiver.waived_hit_count }} 条）</h4>
        <table>
          <thead><tr><th>行号</th><th>实体键</th><th>规则</th><th>严重度</th><th>明细</th><th>豁免单</th></tr></thead>
          <tbody>
            <tr v-for="(h, i) in last.report.waiver.waived_hits" :key="i">
              <td>{{ h.row_no }}</td>
              <td><code>{{ h.entity_key }}</code></td>
              <td>{{ h.rule_name }}</td>
              <td><span class="tag" :class="h.severity === 'error' ? 'danger' : ''">{{ h.severity }}</span></td>
              <td class="muted">{{ h.detail }}</td>
              <td>#{{ h.waiver_id }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
