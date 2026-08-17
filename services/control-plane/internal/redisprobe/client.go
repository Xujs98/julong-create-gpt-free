package redisprobe

import (
	"bufio"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
)

type Client struct {
	address    string
	serverName string
	username   string
	password   string
	database   int
	tls        bool
}

func New(rawURL string) (*Client, error) {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, fmt.Errorf("parse redis URL: %w", err)
	}
	if u.Scheme != "redis" && u.Scheme != "rediss" {
		return nil, fmt.Errorf("unsupported redis URL scheme %q", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return nil, errors.New("redis URL is missing a host")
	}
	port := u.Port()
	if port == "" {
		port = "6379"
	}
	database := 0
	path := strings.TrimPrefix(u.Path, "/")
	if path != "" {
		database, err = strconv.Atoi(path)
		if err != nil || database < 0 {
			return nil, fmt.Errorf("invalid redis database %q", path)
		}
	}
	var username, password string
	if u.User != nil {
		username = u.User.Username()
		var hasPassword bool
		password, hasPassword = u.User.Password()
		if username != "" && !hasPassword {
			return nil, errors.New("redis URL username requires a password")
		}
	}
	return &Client{
		address:    net.JoinHostPort(host, port),
		serverName: host,
		username:   username,
		password:   password,
		database:   database,
		tls:        u.Scheme == "rediss",
	}, nil
}

func (c *Client) Ping(ctx context.Context) error {
	var dialer net.Dialer
	var conn net.Conn
	var err error
	if c.tls {
		conn, err = (&tls.Dialer{NetDialer: &dialer, Config: &tls.Config{ServerName: c.serverName, MinVersion: tls.VersionTLS12}}).DialContext(ctx, "tcp", c.address)
	} else {
		conn, err = dialer.DialContext(ctx, "tcp", c.address)
	}
	if err != nil {
		return err
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}
	r := bufio.NewReader(conn)
	if c.password != "" {
		args := []string{"AUTH", c.password}
		if c.username != "" {
			args = []string{"AUTH", c.username, c.password}
		}
		if err := command(conn, r, "OK", args...); err != nil {
			return fmt.Errorf("redis auth: %w", err)
		}
	}
	if c.database > 0 {
		if err := command(conn, r, "OK", "SELECT", strconv.Itoa(c.database)); err != nil {
			return fmt.Errorf("redis select: %w", err)
		}
	}
	return command(conn, r, "PONG", "PING")
}

func command(w io.Writer, r *bufio.Reader, expected string, args ...string) error {
	var b strings.Builder
	fmt.Fprintf(&b, "*%d\r\n", len(args))
	for _, arg := range args {
		fmt.Fprintf(&b, "$%d\r\n%s\r\n", len([]byte(arg)), arg)
	}
	if _, err := io.WriteString(w, b.String()); err != nil {
		return err
	}
	prefix, err := r.ReadByte()
	if err != nil {
		return err
	}
	line, err := r.ReadString('\n')
	if err != nil {
		return err
	}
	line = strings.TrimSuffix(strings.TrimSuffix(line, "\n"), "\r")
	if prefix == '-' {
		return errors.New(line)
	}
	if prefix != '+' {
		return fmt.Errorf("unexpected redis response prefix %q", prefix)
	}
	if line != expected {
		return fmt.Errorf("unexpected redis response %q", line)
	}
	return nil
}
