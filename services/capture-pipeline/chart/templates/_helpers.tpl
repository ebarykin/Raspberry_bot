{{/*
Expand the name of the chart.
*/}}
{{- define "capture-pipeline.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "capture-pipeline.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "capture-pipeline.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "capture-pipeline.labels" -}}
{{ include "capture-pipeline.selectorLabels" . }}
{{ include "capture-pipeline.ci.labels" . }}
{{- if .Values.global }}
{{ include "global.labels" . }}
helm.sh/subchart: {{ .Chart.Name | quote }}
app.kubernetes.io/module-version: {{ .Chart.AppVersion | quote }}
{{- else }}
helm.sh/chart: {{ include "capture-pipeline.chart" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}
{{- end }}

{{/*
CI/Git Labels used for chart resources
*/}}
{{- define "capture-pipeline.ci.labels" -}}
{{- if .Values.ci }}
{{- if .Values.ci.git.commit }}
ci.io/git-commit: {{ .Values.ci.git.commit | quote }}
{{- end }}
{{- if .Values.ci.git.branch }}
ci.io/git-branch: {{ .Values.ci.git.branch | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "capture-pipeline.selectorLabels" -}}
app.kubernetes.io/name: {{ include "capture-pipeline.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "capture-pipeline.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "capture-pipeline.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
DDS env vars for the chart's own DDS parameters (no global config, or dds.overrideParams: true).
*/}}
{{- define "capture-pipeline.dds.commonEnv" -}}
- name: ROS_DOMAIN_ID
  value: {{ .Values.dds.rosDomainId | quote }}
{{- if eq .Values.dds.type "fastdds" }}
- name: RMW_IMPLEMENTATION
  value: rmw_fastrtps_cpp
- name: FASTRTPS_DEFAULT_PROFILES_FILE
  value: {{ .Values.dds.configPath | default "/rmw/fastdds.xml" }}
{{- else }}
- name: RMW_IMPLEMENTATION
  value: rmw_cyclonedds_cpp
- name: CYCLONEDDS_URI
  value: {{ .Values.dds.configPath | default "/rmw/cyclonedds.xml" }}
{{- end }}
{{- end -}}

{{/*
True when the chart uses the umbrella's global DDS config and ConfigMap.
*/}}
{{- define "capture-pipeline.dds.useGlobal" -}}
{{- if and .Values.global (not .Values.dds.overrideParams) }}true{{ end }}
{{- end -}}

{{/*
DDS env vars: global ones from the umbrella, or the chart's own.
*/}}
{{- define "capture-pipeline.dds.env" -}}
{{- if include "capture-pipeline.dds.useGlobal" . }}
{{- include "global.dds.commonEnv" . }}
{{- else }}
{{- include "capture-pipeline.dds.commonEnv" . }}
{{- end }}
{{- end -}}

{{/*
DDS ConfigMap name: global from the umbrella, or the chart's own.
*/}}
{{- define "capture-pipeline.dds.configmapName" -}}
{{- if include "capture-pipeline.dds.useGlobal" . }}
{{- .Values.global.dds.configmapName }}
{{- else }}
{{- .Values.dds.configmapName }}
{{- end }}
{{- end -}}

{{/*
DDS ConfigMap data. Also used for the checksum/dds-config pod annotation.
*/}}
{{- define "capture-pipeline.dds.data" -}}
{{- if and (include "capture-pipeline.dds.useGlobal" .) (not .Values.dds.fromValues) }}
{{- include "global.dds.config" . }}
{{- else if .Values.dds.fromValues }}
cyclonedds.xml: |
  {{ .Values.dds.cyclonedds.values | nindent 2 }}
fastdds.xml: |
  {{ .Values.dds.fastdds.values | nindent 2 }}
{{- else }}
cyclonedds.xml: |
  {{ .Files.Get "files/dds/cyclonedds.xml" | nindent 2 }}
fastdds.xml: |
  {{ .Files.Get "files/dds/fastdds.xml" | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
True when the umbrella creates the global DDS ConfigMap itself (global.dds.create),
so the chart must not render its own copy of it.
*/}}
{{- define "capture-pipeline.dds.globalCreated" -}}
{{- if and (include "capture-pipeline.dds.useGlobal" .) (dig "dds" "create" false (.Values.global | default dict)) }}true{{ end }}
{{- end -}}

{{- define "capture-pipeline.hostnet" -}}
{{- if .Values.global -}}
{{- if or (and .Values.global.hostNetwork (not .Values.hostNetwork.override))
          (and .Values.hostNetwork.override .Values.hostNetwork.enabled) -}}
dnsPolicy: ClusterFirstWithHostNet
hostNetwork: true
{{- end -}}
{{- else if .Values.hostNetwork.enabled -}}
dnsPolicy: ClusterFirstWithHostNet
hostNetwork: true
{{- end -}}
{{- end -}}

{{- define "capture-pipeline.commonVolume" -}}
- name: dds-config
  configMap:
    name: {{ include "capture-pipeline.dds.configmapName" . }}
{{- if .Values.config.enabled }}
- name: module-config
  configMap:
    name: {{ .Values.config.mapName }}
{{- end -}}
{{- end -}}

{{- define "capture-pipeline.commonVolumeMounts" -}}
- mountPath: /rmw
  name: dds-config
{{- if .Values.config.enabled }}
- mountPath: {{ .Values.config.mountPath }}
  name: module-config
{{- end -}}
{{- end -}}

{{/*
Render "- name: X / value: Y" entries from a plain map of env vars.
*/}}
{{- define "capture-pipeline.envEntries" -}}
{{- range $key, $value := . }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{/*
Render env entries sourced from secrets.
Input: [ { name: <secretName>, secret: { ENV_NAME: <keyInSecret> } }, ... ]
*/}}
{{- define "capture-pipeline.envFromSecretEntries" -}}
{{- range $index, $secret := . }}
{{- range $key, $value := $secret.secret }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret.name | quote }}
      key: {{ $value }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Render a single init container.
Every field is optional except "name" - anything left unset falls back to the
main container's equivalent value from .Values. DDS env and the /rmw mount are
always the same as in the main container.
Input: (dict "root" $ "ic" <one entry of .Values.initContainers>)
*/}}
{{- define "capture-pipeline.initContainer" -}}
{{- $root := .root -}}
{{- $ic := .ic -}}
{{- $values := $root.Values -}}
- name: {{ $ic.name }}
  {{- $defaultImage := printf "%s:%s" $values.image.repository ($values.image.tag | default $root.Chart.AppVersion) }}
  image: "{{ $ic.image | default $defaultImage }}"
  imagePullPolicy: {{ $ic.imagePullPolicy | default $values.image.pullPolicy }}
  securityContext:
    {{- toYaml ($ic.securityContext | default $values.securityContext) | nindent 4 }}
  {{- $command := $ic.command | default $values.command }}
  {{- if $command }}
  {{- if kindIs "string" $command }}
  {{- $command = list $command }}
  {{- end }}
  command:
    {{- range $command }}
    - {{ . | quote }}
    {{- end }}
  {{- end }}
  {{- $args := $ic.arguments | default $values.arguments }}
  {{- if $args }}
  {{- if kindIs "string" $args }}
  {{- $args = list $args }}
  {{- end }}
  args:
    {{- range $args }}
    - {{ . | quote }}
    {{- end }}
  {{- end }}
  resources:
    {{- toYaml ($ic.resources | default $values.resources) | nindent 4 }}
  volumeMounts:
    {{- include "capture-pipeline.commonVolumeMounts" $root | nindent 4 }}
    {{- range $configmap := $values.configmaps }}
    - name: {{ $configmap.configMapName }}
      mountPath: {{ $configmap.mountPath }}
    {{- end }}
    {{- with ($ic.volumeMounts | default $values.volumeMounts) }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
    {{- with ($ic.extraVolumeMounts | default $values.extraVolumeMounts) }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  env:
    {{- if $values.global }}
    {{- include "capture-pipeline.envEntries" $values.global.env | nindent 4 }}
    {{- include "capture-pipeline.envFromSecretEntries" $values.global.envFromSecrets | nindent 4 }}
    {{- end }}
    {{- include "capture-pipeline.dds.env" $root | nindent 4 }}
    {{- include "capture-pipeline.envEntries" ($ic.env | default $values.env) | nindent 4 }}
    {{- include "capture-pipeline.envFromSecretEntries" ($ic.envFromSecrets | default $values.envFromSecrets) | nindent 4 }}
{{- end -}}
