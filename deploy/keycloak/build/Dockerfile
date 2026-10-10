FROM eclipse-temurin:17-jdk
RUN useradd -r -u 1001 -d /opt/keycloak -s /usr/sbin/nologin keycloak
COPY --chown=1001:1000 keycloak /opt/keycloak
USER 1001
EXPOSE 8080 8443 9000
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start"]
