/** Sends one Telegram summary when an observed outage has fully recovered. */
class AllOnlineNotifier {
    /**
     * @param {object} services Persistence and notification adapters
     */
    constructor(services) {
        this.services = services;
        this.queues = new Map();
        this.startedAt = services.now();
    }

    /**
     * Serialize decisions per owner without delaying monitor scheduling.
     * @param {number} userID Monitor owner
     * @param {number} status Stored heartbeat status
     * @param {?object} event Stored monitor ID and observation time
     * @returns {Promise<void>} Completion, with failures logged safely
     */
    record(userID, status, event = null) {
        const observedAt = this.services.now();
        const previous = this.queues.get(userID) || Promise.resolve();
        const task = previous.then(() => this.evaluate(userID, status, observedAt, event)).catch(() => {
            // Provider errors can contain bot tokens or URLs. Do not log them.
            this.services.log("Cannot update all-online summary; will retry on a later heartbeat");
        });
        this.queues.set(userID, task);
        task.then(() => {
            if (this.queues.get(userID) === task) {
                this.queues.delete(userID);
            }
        });
        return task;
    }

    /**
     * Persist the outage latch and deliver only after every active device is UP.
     * @param {number} userID Monitor owner
     * @param {number} status Stored heartbeat status
     * @param {number} observedAt Time the heartbeat was stored
     * @param {?object} event Stored monitor ID and observation time
     * @returns {Promise<void>} Completion
     */
    async evaluate(userID, status, observedAt, event) {
        const s = this.services;
        const state = await s.loadState(userID) || { armed: false };
        if (status === 0) {
            // A check started before a later successful check can finish last.
            // Only the newest observation may open an outage, never the last row ID.
            if (event && !await s.isLatestDown(userID, event.id, event.time)) {
                return;
            }
            // A queued DOWN already covered by a newer recovery snapshot must
            // not open a second outage after concurrent devices recovered.
            if (observedAt <= (state.lastRecoveryAt || 0)) {
                return;
            }
            if (!state.armed || (state.recoverySnapshotAt && observedAt > state.recoverySnapshotAt)) {
                await s.storeState(userID, {
                    armed: true,
                    outageStartedAt: state.armed ? state.outageStartedAt : observedAt,
                    delivered: [],
                });
            }
            return;
        }
        if (!state.armed || status !== 1) {
            return;
        }
        if (state.lastAttemptAt && s.now() - state.lastAttemptAt < 60000) {
            return;
        }

        const snapshotAt = s.now();
        const monitors = await s.loadMonitors(userID);
        if (!monitors.length || monitors.some((m) => {
            const checkedAt = Date.parse(m.time + "Z");
            return Number(m.status) !== 1 || !Number.isFinite(checkedAt) || checkedAt < this.startedAt;
        })) {
            return;
        }
        const notifications = (await s.loadNotifications(userID)).filter((n) => n.config.type === "telegram");
        if (!notifications.length) {
            return;
        }

        state.lastAttemptAt = snapshotAt;
        state.recoverySnapshotAt = snapshotAt;
        state.delivered = state.delivered || [];
        await s.storeState(userID, state);
        const onlineCount = monitors.filter((monitor) => Number(monitor.status) === 1).length;
        const offlineCount = monitors.filter((monitor) => Number(monitor.status) === 0).length;
        const localTime = new Date(snapshotAt).toLocaleString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh" });
        const message = "<b>✅ ALL SYSTEMS ONLINE</b>\n" +
            "━━━━━━━━━━━━━━\n" +
            `🟢 Online: <b>${onlineCount}</b>\n` +
            `🔴 Offline: <b>${offlineCount}</b>\n\n` +
            `🕒 ${localTime}\n` +
            "━━━━━━━━━━━━━━\n" +
            "<i>UPTIME KUMA • MONITORING</i>";
        for (const notification of notifications) {
            if (state.delivered.includes(notification.id)) {
                continue;
            }
            try {
                // Use the existing destination, thread, proxy and silent-send
                // settings while formatting this summary in Telegram HTML.
                await s.send({
                    ...notification.config,
                    telegramUseTemplate: true,
                    telegramTemplateParseMode: "HTML",
                    telegramTemplate: message,
                }, message);
            } catch (_) {
                s.log(`Telegram all-online summary failed for notification #${notification.id}; retry in 60s`);
                continue;
            }
            state.delivered.push(notification.id);
            await s.storeState(userID, state);
            s.log(`Telegram all-online summary delivered: ${monitors.length} devices, notification #${notification.id}`);
        }
        if (notifications.every((n) => state.delivered.includes(n.id))) {
            await s.storeState(userID, {
                armed: false,
                lastRecoveryAt: snapshotAt,
                onlineCount: monitors.length,
            });
        }
    }
}

let notifier;

/**
 * Record only a heartbeat already committed to the database.
 * @param {object} monitor Monitor bean
 * @param {object} heartbeat Heartbeat bean
 * @returns {Promise<void>} Background summary evaluation
 */
function recordHeartbeat(monitor, heartbeat) {
    if (monitor.type === "group" || monitor.isStop) {
        return Promise.resolve();
    }
    if (!notifier) {
        const { R } = require("redbean-node");
        const { Settings } = require("./settings");
        const { Notification } = require("./notification");
        const { log } = require("../src/util");
        const key = (userID) => `allOnlineSummary:${userID}`;
        log.info("all-online", "Telegram all-online summary enabled; waiting for an observed device outage");
        notifier = new AllOnlineNotifier({
            now: () => Date.now(),
            loadState: (userID) => Settings.get(key(userID)),
            storeState: (userID, state) => Settings.set(key(userID), state, "all-online-summary"),
            isLatestDown: async (userID, monitorID, time) => {
                const latest = await R.getRow(
                    "SELECT h.status, h.time FROM monitor m JOIN heartbeat h " +
                    "ON h.id = (SELECT id FROM heartbeat WHERE monitor_id = m.id ORDER BY time DESC, id DESC LIMIT 1) " +
                    "WHERE m.id = ? AND m.user_id = ? AND m.active = 1 AND m.type <> 'group'", [monitorID, userID]
                );
                return latest && Number(latest.status) === 0 && latest.time === time;
            },
            loadMonitors: (userID) => R.getAll(
                "SELECT m.id, h.status, h.time FROM monitor m LEFT JOIN heartbeat h " +
                "ON h.id = (SELECT id FROM heartbeat WHERE monitor_id = m.id ORDER BY time DESC, id DESC LIMIT 1) " +
                "WHERE m.user_id = ? AND m.active = 1 AND m.type <> 'group'", [userID]
            ),
            loadNotifications: async (userID) => {
                const rows = await R.getAll(
                    "SELECT DISTINCT n.id, n.config FROM notification n " +
                    "JOIN monitor_notification mn ON mn.notification_id = n.id " +
                    "JOIN monitor m ON m.id = mn.monitor_id " +
                    "WHERE n.user_id = ? AND n.active = 1 AND m.user_id = ? " +
                    "AND m.active = 1 AND m.type <> 'group'", [userID, userID]
                );
                return rows.map((n) => ({ id: n.id, config: JSON.parse(n.config) }));
            },
            send: (config, message) => Notification.send(config, message),
            log: (message) => log.info("all-online", message),
        });
    }
    return notifier.record(monitor.user_id, heartbeat.status, { id: monitor.id, time: heartbeat.time });
}

module.exports = { AllOnlineNotifier, recordHeartbeat };
