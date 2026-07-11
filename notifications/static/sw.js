/* Service Worker for browser push notifications */

self.addEventListener('push', function (event) {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'Trading Alert';
    const options = {
        body: data.body || '',
        icon: '/static/icon.png',
        badge: '/static/badge.png',
        tag: 'trading-alert-' + (data.notification_id || Date.now()),
        renotify: true,
        requireInteraction: true,
        data: { notification_id: data.notification_id },
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();

    // Mark notification read via fetch and open/focus the dashboard
    const notificationId = event.notification.data && event.notification.data.notification_id;

    event.waitUntil(
        (async () => {
            if (notificationId) {
                try {
                    await fetch('/notifications/mark-read', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ id: notificationId }),
                    });
                } catch (_err) {
                    // Non-critical — best effort
                }
            }

            const allClients = await clients.matchAll({ type: 'window', includeUncontrolled: true });
            const dashboardClient = allClients.find(c => c.url.includes('/wave-extractor'));
            if (dashboardClient) {
                dashboardClient.focus();
            } else {
                clients.openWindow('/wave-extractor');
            }
        })()
    );
});
