package redisprobe

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"testing"
	"time"
)

func TestNewParsesRedisURL(t *testing.T) {
	c, err := New("rediss://user:pass@example.test:6380/4")
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if c.address != "example.test:6380" || c.serverName != "example.test" || c.username != "user" || c.password != "pass" || c.database != 4 || !c.tls {
		t.Fatalf("unexpected client: %+v", c)
	}
}

func TestNewKeepsIPv6TLSServerName(t *testing.T) {
	c, err := New("rediss://[2001:db8::1]:6380/0")
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	if c.address != "[2001:db8::1]:6380" || c.serverName != "2001:db8::1" {
		t.Fatalf("unexpected IPv6 client: %+v", c)
	}
}

func TestNewRejectsUsernameWithoutPassword(t *testing.T) {
	if _, err := New("redis://user@localhost:6379/0"); err == nil {
		t.Fatal("New() accepted username without password")
	}
}

func TestPingSpeaksRESP(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	serverErr := make(chan error, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			serverErr <- err
			return
		}
		defer conn.Close()
		r := bufio.NewReader(conn)
		line, err := r.ReadString('\n')
		if err != nil {
			serverErr <- err
			return
		}
		if line != "*1\r\n" {
			serverErr <- fmt.Errorf("unexpected command header %q", line)
			return
		}
		line, err = r.ReadString('\n')
		if err != nil {
			serverErr <- err
			return
		}
		if line != "$4\r\n" {
			serverErr <- fmt.Errorf("unexpected command length %q", line)
			return
		}
		line, err = r.ReadString('\n')
		if err != nil {
			serverErr <- err
			return
		}
		if line != "PING\r\n" {
			serverErr <- fmt.Errorf("unexpected command %q", line)
			return
		}
		if _, err := conn.Write([]byte("+PONG\r\n")); err != nil {
			serverErr <- err
			return
		}
		serverErr <- nil
	}()

	c, err := New("redis://" + listener.Addr().String() + "/0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := c.Ping(ctx); err != nil {
		t.Fatalf("Ping() error = %v", err)
	}
	if err := <-serverErr; err != nil {
		t.Fatal(err)
	}
}
