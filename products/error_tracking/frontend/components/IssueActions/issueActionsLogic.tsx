import { actions, kea, listeners, path } from 'kea'
import api from 'lib/api'
import posthog from 'posthog-js'

import { ErrorTrackingIssue } from '~/queries/schema/schema-general'

import type { issueActionsLogicType } from './issueActionsLogicType'

export const issueActionsLogic = kea<issueActionsLogicType>([
    path(['scenes', 'error-tracking', 'issueActionsLogic']),

    actions({
        mergeIssues: (ids: string[]) => ({ ids }),
        resolveIssues: (ids: string[]) => ({ ids }),
        suppressIssues: (ids: string[]) => ({ ids }),
        activateIssues: (ids: string[]) => ({ ids }),
        assignIssues: (ids: string[], assignee: ErrorTrackingIssue['assignee']) => ({ ids, assignee }),

        updateIssueAssignee: (id: string, assignee: ErrorTrackingIssue['assignee']) => ({ id, assignee }),
        updateIssueStatus: (id: string, status: ErrorTrackingIssue['status']) => ({ id, status }),
        updateIssueName: (id: string, name: string) => ({ id, name }),
        updateIssueDescription: (id: string, description: string) => ({ id, description }),

        mutationSuccess: () => {},
        mutationFailure: (error: unknown) => ({ error }),
    }),

    listeners(({ actions }) => {
        async function runMutation(cb: () => Promise<void>): Promise<void> {
            try {
                await cb()
                actions.mutationSuccess()
            } catch (e: unknown) {
                actions.mutationFailure(e)
            }
        }
        return {
            mergeIssues: async ({ ids }) => {
                const [firstId, ...otherIds] = ids
                if (firstId && otherIds.length > 0) {
                    await runMutation(async () => {
                        posthog.capture('error_tracking_issue_merged', { primary: firstId })
                        await api.errorTracking.mergeInto(firstId, otherIds)
                    })
                }
            },
            resolveIssues: async ({ ids }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_bulk_resolve')
                    await api.errorTracking.bulkMarkStatus(ids, 'resolved')
                })
            },
            suppressIssues: async ({ ids }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_bulk_suppress')
                    await api.errorTracking.bulkMarkStatus(ids, 'suppressed')
                })
            },
            activateIssues: async ({ ids }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_bulk_activate')
                    await api.errorTracking.bulkMarkStatus(ids, 'active')
                })
            },
            assignIssues: async ({ ids, assignee }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_bulk_assign')
                    await api.errorTracking.bulkAssign(ids, assignee)
                })
            },
            updateIssueAssignee: async ({ id, assignee }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_update_assignee')
                    await api.errorTracking.assignIssue(id, assignee)
                })
            },
            updateIssueStatus: async ({ id, status }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_update_status')
                    await api.errorTracking.updateIssue(id, { status })
                })
            },
            updateIssueName: async ({ id, name }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_update_name')
                    await api.errorTracking.updateIssue(id, { name })
                })
            },
            updateIssueDescription: async ({ id, description }) => {
                await runMutation(async () => {
                    posthog.capture('error_tracking_issue_update_description')
                    await api.errorTracking.updateIssue(id, { description })
                })
            },
        }
    }),
])
