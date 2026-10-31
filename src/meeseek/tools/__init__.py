"""Raw capability adapters (GitLab/Jira REST, DCI, Google Drive/Docs, mermaid).

Tools are attachments to skills: they carry no SOP of their own. Skills
(skills/<name>/SKILL.md) declare which tools they use by name, and the
runtime layer (tools.registry) binds these adapters into tool specs for the
agent loop.
"""
